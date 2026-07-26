//! Shared secret-scrubbing rules for the persistence sinks.
//!
//! One home for the value-pattern redaction that every persistence sink must
//! apply. Ported from `model/redaction.py`: the pattern set lives here in
//! `model/` where both the kernel (transcript + metadata) and the commands
//! (`/export` + `/copy`) can share the *same* definition rather than fork
//! four copies.
//!
//! Two complementary layers cover secrets on disk / clipboard:
//!
//! - **Key-based** redaction (amplifier-core's `redact_secrets`) scrubs
//!   structured *metadata* by sensitive KEY name. It stays kernel-side.
//! - **Value-pattern** redaction (this module) scrubs secret-shaped *values*
//!   (AWS keys, bearer tokens, private-key blocks, provider tokens) out of
//!   free text — the transcript bodies, exported markdown and copied answers
//!   that key redaction never sees.
//!
//! Redaction is idempotent: the placeholder never matches a rule, so
//! scrubbing already-scrubbed text is a no-op (safe to re-run on
//! resume/re-export).

use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

/// Marker written in place of a matched secret. Deliberately identical to
/// amplifier-core's key-based placeholder so metadata and free text read the
/// same on disk.
pub const REDACTION_PLACEHOLDER: &str = "[REDACTED]";

/// Each rule is `(pattern, replacement)`; `pattern.replace_all(text, replacement)`
/// runs in order. Replacements that keep a non-secret prefix (auth scheme,
/// assignment `key =`) capture it so surrounding context survives the scrub.
static RULES: LazyLock<Vec<(Regex, String)>> = LazyLock::new(|| {
    vec![
        // PEM private-key blocks (any label) — redact header..footer as a unit.
        (
            Regex::new(
                r"(?s)-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            )
            .expect("valid PEM rule"),
            REDACTION_PLACEHOLDER.to_string(),
        ),
        // AWS access key IDs (AKIA/ASIA/… + 16 base32 chars), incl. the AWS
        // docs example key AKIAIOSFODNN7EXAMPLE.
        (
            Regex::new(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|A3T[A-Z0-9])[A-Z0-9]{16}\b")
                .expect("valid AWS key-id rule"),
            REDACTION_PLACEHOLDER.to_string(),
        ),
        // AWS secret access keys are 40 base64 chars — too generic to match on
        // shape alone, so only when introduced by their canonical key name.
        (
            Regex::new(r#"(?i)(aws_secret_access_key\s*[:=]\s*)['"]?[A-Za-z0-9/+=]{40}['"]?"#)
                .expect("valid AWS secret rule"),
            format!("${{1}}{REDACTION_PLACEHOLDER}"),
        ),
        // GitHub tokens (PAT/OAuth/app/refresh + fine-grained pat).
        (
            Regex::new(r"\bgh[posur]_[A-Za-z0-9]{36,}\b").expect("valid GitHub token rule"),
            REDACTION_PLACEHOLDER.to_string(),
        ),
        (
            Regex::new(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b").expect("valid GitHub PAT rule"),
            REDACTION_PLACEHOLDER.to_string(),
        ),
        // Google API keys.
        (
            Regex::new(r"\bAIza[A-Za-z0-9_\-]{35}\b").expect("valid Google key rule"),
            REDACTION_PLACEHOLDER.to_string(),
        ),
        // Slack tokens.
        (
            Regex::new(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b").expect("valid Slack token rule"),
            REDACTION_PLACEHOLDER.to_string(),
        ),
        // Bearer / Token auth credentials — keep the scheme, drop the credential.
        (
            Regex::new(r"(?i)\b(Bearer|Token)\s+[A-Za-z0-9._~+/\-]{8,}=*")
                .expect("valid bearer rule"),
            format!("${{1}} {REDACTION_PLACEHOLDER}"),
        ),
        // Labeled secret assignments: `api_key = …`, `password: …`,
        // `client_secret=…` — the catch-all for named credentials whose value
        // has no distinctive shape. Value must be >=6 non-space chars.
        (
            Regex::new(
                r#"(?im)^(?P<pre>[^\n:=]*(?:secret|token|password|passwd|api[_-]?key|access[_-]?key|client[_-]?secret|credential)[^\n:=]*\s*[:=]\s*)['"]?(?P<val>[^\s'"]{6,})['"]?[ \t]*$"#,
            )
            .expect("valid labeled-assignment rule"),
            format!("${{pre}}{REDACTION_PLACEHOLDER}"),
        ),
    ]
});

/// Return `text` with every secret-shaped substring replaced.
///
/// Idempotent: the placeholder matches no rule, so re-scrubbing is a no-op.
pub fn scrub_text(text: &str) -> String {
    let mut scrubbed = text.to_string();
    for (pattern, replacement) in RULES.iter() {
        scrubbed = pattern
            .replace_all(&scrubbed, replacement.as_str())
            .into_owned();
    }
    scrubbed
}

/// Recursively scrub every string leaf of `value`.
///
/// Walks JSON objects/arrays (the shape of a sanitized transcript message or
/// redacted metadata dict) and applies [`scrub_text`] to each string leaf.
/// Non-string, non-container leaves pass through unchanged. Keys are left
/// as-is — key-based redaction owns those.
pub fn scrub_value(value: Value) -> Value {
    match value {
        Value::String(text) => Value::String(scrub_text(&text)),
        Value::Object(map) => Value::Object(
            map.into_iter()
                .map(|(key, item)| (key, scrub_value(item)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.into_iter().map(scrub_value).collect()),
        other => other,
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    // A fake AWS key + secret pair (the AWS docs example key — not a live secret).
    const AWS_KEY: &str = "AKIAIOSFODNN7EXAMPLE";
    const AWS_SECRET: &str = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";
    const BEARER: &str = "Bearer eyJhbGciOi.J9pay.load-sig_nature123";

    #[test]
    fn test_aws_access_key_id_is_redacted() {
        let out = scrub_text(&format!("the key is {AWS_KEY} ok"));
        assert!(!out.contains(AWS_KEY));
        assert_eq!(out, format!("the key is {REDACTION_PLACEHOLDER} ok"));
    }

    #[test]
    fn test_aws_secret_access_key_line_is_redacted() {
        let out = scrub_text(&format!("aws_secret_access_key = {AWS_SECRET}"));
        assert!(!out.contains(AWS_SECRET));
        assert_eq!(out, format!("aws_secret_access_key = {REDACTION_PLACEHOLDER}"));
    }

    #[test]
    fn test_bearer_token_redacted_but_scheme_kept() {
        let out = scrub_text(&format!("Authorization: {BEARER}"));
        assert!(!out.contains("eyJhbGci"));
        assert_eq!(out, format!("Authorization: Bearer {REDACTION_PLACEHOLDER}"));
    }

    #[test]
    fn test_provider_tokens_are_redacted() {
        // Fixtures are built by concatenation so repo secret scanners
        // (e.g. GitHub push protection) don't match the source literals.
        let secrets = [
            format!("{}{}", "ghp_", "1234567890abcdefghij1234567890ABCDEF"),
            format!("{}{}", "github_pat_", "11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz"),
            format!("{}{}", "AIzaSy", "A1234567890abcdefghijklmnopqrstuvw"),
            format!("{}{}", "xoxb-", "1234567890-abcdefghijklmnop"),
        ];
        for secret in &secrets {
            let out = scrub_text(&format!("token {secret} trailing"));
            assert!(!out.contains(secret.as_str()), "secret survived: {secret}");
            assert!(out.contains(REDACTION_PLACEHOLDER));
        }
    }

    #[test]
    fn test_pem_private_key_block_is_redacted() {
        let pem = "-----BEGIN RSA PRIVATE KEY-----\n\
                   MIIEpAIBAAKCAQEA1234567890\nabcdefgh\n\
                   -----END RSA PRIVATE KEY-----";
        let out = scrub_text(&format!("key:\n{pem}\ndone"));
        assert!(!out.contains("MIIEpAIBAAKCAQEA"));
        assert_eq!(out, format!("key:\n{REDACTION_PLACEHOLDER}\ndone"));
    }

    #[test]
    fn test_labeled_secret_assignment_is_redacted() {
        let out = scrub_text("api_key = sk-supersecretvalue123");
        assert!(!out.contains("supersecret"));
        assert_eq!(out, format!("api_key = {REDACTION_PLACEHOLDER}"));
    }

    #[test]
    fn test_full_credentials_file_round_trips_redacted() {
        let creds = format!(
            "[default]\naws_access_key_id = {AWS_KEY}\naws_secret_access_key = {AWS_SECRET}\n"
        );
        let out = scrub_text(&creds);
        assert!(!out.contains(AWS_KEY));
        assert!(!out.contains(AWS_SECRET));
        assert_eq!(out.matches(REDACTION_PLACEHOLDER).count(), 2);
    }

    #[test]
    fn test_benign_text_is_untouched() {
        let text = "Please fix the flaky test in app.py; the access_key parameter is fine.";
        assert_eq!(scrub_text(text), text);
    }

    #[test]
    fn test_scrubbing_is_idempotent() {
        let once = scrub_text(&format!(
            "key {AWS_KEY} and aws_secret_access_key = {AWS_SECRET}"
        ));
        assert_eq!(scrub_text(&once), once);
    }

    #[test]
    fn test_scrub_value_recurses_nested_containers() {
        // Python's tuple leaf maps to a JSON array here (serde_json has no
        // tuple type); the scrub behavior over the sequence is identical.
        let message = json!({
            "role": "user",
            "content": [
                {"type": "text", "text": format!("my key is {AWS_KEY}")},
                {"type": "text", "text": "harmless"},
            ],
            "meta": ["tag", format!("Bearer {AWS_KEY}")],
        });
        let scrubbed = scrub_value(message);
        assert_eq!(
            scrubbed["content"][0]["text"],
            json!(format!("my key is {REDACTION_PLACEHOLDER}"))
        );
        assert_eq!(scrubbed["content"][1]["text"], json!("harmless"));
        assert_eq!(
            scrubbed["meta"],
            json!(["tag", format!("Bearer {REDACTION_PLACEHOLDER}")])
        );
        // role/keys preserved, non-str leaves pass through
        assert_eq!(scrubbed["role"], json!("user"));
    }

    #[test]
    fn test_scrub_value_passes_through_non_string_leaves() {
        assert_eq!(
            scrub_value(json!({"n": 1, "ok": true, "x": null})),
            json!({"n": 1, "ok": true, "x": null})
        );
    }
}
