"""Tests for kernel/session_factory.py — canonical session initialization.

All tests use fakes for the PreparedBundle / AmplifierSession surface:
no API keys, no network, no real module mounting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplifier_app_tui.kernel.config import ResolvedConfig
from amplifier_app_tui.kernel.session_factory import (
    APPLICATION_HOST,
    RESUME_CAPABILITY,
    SPAWN_CAPABILITY,
    ProviderMountError,
    SessionRequest,
    create_initialized_session,
    missing_tool_modules,
    stamp_root_metadata,
    verify_mounts,
)

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeContext:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages = list(messages or [])

    async def get_messages(self) -> list[dict[str, Any]]:
        return list(self.messages)

    async def set_messages(self, messages: list[dict[str, Any]]) -> None:
        self.messages = list(messages)


class FakeCoordinator:
    def __init__(
        self,
        providers: dict[str, Any] | None = None,
        tools: dict[str, Any] | None = None,
        context: FakeContext | None = None,
    ) -> None:
        self._modules: dict[str, Any] = {
            "providers": providers or {},
            "tools": tools or {},
            "context": context,
        }
        self.capabilities: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        return self._modules.get(name)

    def register_capability(self, name: str, capability: Any) -> None:
        self.capabilities[name] = capability


class FakeSession:
    def __init__(self, coordinator: FakeCoordinator) -> None:
        self.coordinator = coordinator
        self.config: dict[str, Any] = {}
        self.cleaned = False

    async def cleanup(self) -> None:
        self.cleaned = True


class FakePrepared:
    def __init__(
        self,
        session: FakeSession,
        mount_plan: dict[str, Any],
        module_exports: dict[str, list[str]] | None = None,
    ) -> None:
        self.session = session
        self.mount_plan = mount_plan
        # Mirrors amplifier_foundation.bundle.PreparedBundle.module_exports
        # (module_id → registered tool names) so verify_mounts can name a
        # single failed multi-tool module.
        self.module_exports: dict[str, list[str]] = module_exports or {}
        self.create_kwargs: dict[str, Any] = {}

    async def create_session(self, **kwargs: Any) -> FakeSession:
        self.create_kwargs = kwargs
        return self.session


# Matches the real foundation KNOWN_MODULE_EXPORTS shape for the two
# multi-tool modules the fixtures use.
FAKE_MODULE_EXPORTS: dict[str, list[str]] = {
    "tool-filesystem": ["read_file", "write_file", "edit_file", "glob"],
}


def make_resolved(
    session: FakeSession,
    mount_plan: dict[str, Any],
    project_dir: Path,
    module_exports: dict[str, list[str]] | None = None,
) -> ResolvedConfig:
    prepared = FakePrepared(session, mount_plan, module_exports)
    return ResolvedConfig(
        bundle_name="testbundle",
        bundle_uri="file:///test/bundle.md",
        settings={},
        prepared=prepared,
        mount_plan=prepared.mount_plan,
        project_dir=project_dir,
    )


def healthy_setup(tmp_path: Path) -> tuple[FakeSession, ResolvedConfig]:
    mount_plan = {
        "providers": [{"module": "provider-anthropic", "config": {}}],
        "tools": [{"module": "tool-filesystem"}, {"module": "tool-bash"}],
    }
    coordinator = FakeCoordinator(
        providers={"anthropic": object()},
        tools={"read_file": object(), "write_file": object(), "bash": object()},
        context=FakeContext(),
    )
    session = FakeSession(coordinator)
    return session, make_resolved(session, mount_plan, tmp_path, FAKE_MODULE_EXPORTS)


# --------------------------------------------------------------------------
# stamp_root_metadata
# --------------------------------------------------------------------------


def test_stamp_root_metadata_fills_and_guards(tmp_path: Path) -> None:
    config: dict[str, Any] = {"root_session_id": "parent-root"}
    stamp_root_metadata(config, session_id="child", bundle_name="b", project_dir=tmp_path)
    assert config["root_session_id"] == "parent-root"  # guard: never overwritten
    assert config["application_host"] == APPLICATION_HOST
    assert config["bundle_name"] == "b"
    assert config["working_dir"] == str(tmp_path.resolve())
    assert config["project_name"] == tmp_path.name
    assert config["project_slug"].startswith("-")


# --------------------------------------------------------------------------
# missing_tool_modules (pure)
# --------------------------------------------------------------------------


def test_missing_tool_modules_none_missing_via_short_name() -> None:
    # No exports map: single-tool modules resolve by the tool-bash → bash
    # convention; both present ⇒ nothing missing.
    assert missing_tool_modules(["tool-bash", "tool-grep"], ["bash", "grep"]) == ()


def test_missing_tool_modules_names_the_absent_module() -> None:
    assert missing_tool_modules(["tool-bash", "tool-team-pulse"], ["bash"]) == ("tool-team-pulse",)


def test_missing_tool_modules_normalizes_hyphen_underscore() -> None:
    # module short name 'team-pulse' matches a mounted 'team_pulse' tool.
    assert missing_tool_modules(["tool-team-pulse"], ["team_pulse"]) == ()


def test_missing_tool_modules_prefix_boundary_match() -> None:
    # 'web' is an underscore-delimited prefix of the mounted 'web_search'.
    assert missing_tool_modules(["tool-web"], ["web_search"]) == ()
    # but 'bash' must NOT match 'bash_extras' as a bare substring — the
    # boundary is the underscore, and here 'bash' itself is absent.
    assert missing_tool_modules(["tool-bash"], ["bash_extras"]) == ()  # bash_ prefix ⇒ present


def test_missing_tool_modules_exports_win_over_short_name() -> None:
    exports = {"tool-filesystem": ["read_file", "write_file"]}
    # short-name 'filesystem' is absent, but an exported tool mounted ⇒ present.
    assert missing_tool_modules(["tool-filesystem"], ["read_file"], exports) == ()
    # none of the exported tools mounted ⇒ named as failed.
    assert missing_tool_modules(["tool-filesystem"], ["bash"], exports) == ("tool-filesystem",)


def test_missing_tool_modules_unpredictable_name_not_falsely_flagged() -> None:
    # tool-fake mounts a tool named 'write_file' (its short name 'fake' is
    # NOT the registered name, and it's absent from module_exports). Since
    # 'write_file' is unattributed, the short-name fallback is unreliable —
    # tool-fake must NOT be convicted just because 'fake' is absent.
    assert missing_tool_modules(["tool-fake"], ["write_file"]) == ()


def test_missing_tool_modules_authoritative_verdict_holds_despite_unattributed() -> None:
    # tool-filesystem is authoritative (in exports); none of its exports
    # mounted ⇒ failed, even though the unrelated 'write_file' is unattributed.
    exports = {"tool-filesystem": ["read_file", "glob"]}
    assert missing_tool_modules(["tool-filesystem"], ["write_file"], exports) == (
        "tool-filesystem",
    )


def test_missing_tool_modules_conditional_module_not_convicted_when_silent() -> None:
    # tool-mcp mounts fine but registers ZERO tools without ~/.amplifier/mcp.json
    # — its documented no-op state. Every other module here is attributable, so
    # the short-name fallback IS reliable and would otherwise convict it: this is
    # the exact shape of the spurious "degraded start · tool-mcp" on a clean install.
    assert missing_tool_modules(["tool-bash", "tool-mcp"], ["bash"]) == ()


def test_missing_tool_modules_conditional_module_still_matched_when_active() -> None:
    # With servers configured tool-mcp registers mcp_<server>_<tool>; the prefix
    # match still attributes those, so the allowlist changes nothing here.
    assert missing_tool_modules(["tool-mcp"], ["mcp_github_search"]) == ()


def test_missing_tool_modules_conditional_allowlist_does_not_shield_others() -> None:
    # Only tool-mcp is excused. tool-bash is configured so 'bash' is attributed
    # and the short-name fallback stays reliable — tool-team-pulse is then
    # genuinely convicted, exactly as it would be without tool-mcp present.
    assert missing_tool_modules(["tool-bash", "tool-mcp", "tool-team-pulse"], ["bash"]) == (
        "tool-team-pulse",
    )


# --------------------------------------------------------------------------
# verify_mounts
# --------------------------------------------------------------------------


def test_verify_mounts_all_healthy(tmp_path: Path) -> None:
    session, resolved = healthy_setup(tmp_path)
    report = verify_mounts(resolved.mount_plan, session.coordinator, FAKE_MODULE_EXPORTS)
    assert report.missing_providers == ()
    assert report.missing_tools == ()
    assert not report.tools_degraded
    assert report.degraded_notice() is None


def test_verify_mounts_missing_provider_normalizes_names() -> None:
    mount_plan = {"providers": [{"module": "provider-anthropic"}, {"module": "provider-openai"}]}
    coordinator = FakeCoordinator(providers={"anthropic": object()})
    report = verify_mounts(mount_plan, coordinator)
    assert report.missing_providers == ("openai",)


def test_verify_mounts_multi_instance_counted() -> None:
    mount_plan = {
        "providers": [
            {"module": "provider-anthropic", "instance_id": "a1"},
            {"module": "provider-anthropic", "instance_id": "a2"},
        ]
    }
    coordinator = FakeCoordinator(providers={"a1": object()})
    report = verify_mounts(mount_plan, coordinator)
    # two configured instances, one mounted — one missing
    assert len(report.missing_providers) == 1


def test_verify_mounts_complete_tool_failure_lists_modules() -> None:
    mount_plan = {"tools": [{"module": "tool-filesystem"}, {"module": "tool-bash"}]}
    coordinator = FakeCoordinator(tools={})
    report = verify_mounts(mount_plan, coordinator)
    assert report.missing_tools == ("tool-filesystem", "tool-bash")
    assert report.tools_degraded
    notice = report.degraded_notice()
    assert notice is not None and "tool-filesystem" in notice


def test_verify_mounts_single_failed_tool_module_is_named() -> None:
    # The RCA case: tool-team-pulse fails to mount while tool-bash succeeds.
    # coordinator.get("tools") is non-empty, so a name-count diff would miss
    # it — the per-module diff names the exact module that failed.
    mount_plan = {"tools": [{"module": "tool-bash"}, {"module": "tool-team-pulse"}]}
    coordinator = FakeCoordinator(tools={"bash": object()})
    report = verify_mounts(mount_plan, coordinator)
    assert report.missing_tools == ("tool-team-pulse",)
    assert report.tools_degraded
    notice = report.degraded_notice()
    assert notice is not None and "tool-team-pulse" in notice


def test_verify_mounts_partial_multitool_module_named_via_exports() -> None:
    # tool-filesystem registers read_file/write_file/... ; only 'bash' mounted
    # ⇒ filesystem contributed none of its tools ⇒ named as failed. Uses the
    # module_exports map so a multi-tool module isn't judged by its short name.
    mount_plan = {"tools": [{"module": "tool-filesystem"}, {"module": "tool-bash"}]}
    coordinator = FakeCoordinator(tools={"bash": object()})
    report = verify_mounts(mount_plan, coordinator, FAKE_MODULE_EXPORTS)
    assert report.missing_tools == ("tool-filesystem",)
    assert report.tools_degraded
    notice = report.degraded_notice()
    assert notice is not None and "tool-filesystem" in notice


def test_verify_mounts_multitool_module_present_if_any_tool_mounts() -> None:
    # tool-filesystem mounted only one of its tools (edit_file) — that is a
    # benign partial, NOT a module failure (app-cli's lesson). Not named.
    mount_plan = {"tools": [{"module": "tool-filesystem"}]}
    coordinator = FakeCoordinator(tools={"edit_file": object()})
    report = verify_mounts(mount_plan, coordinator, FAKE_MODULE_EXPORTS)
    assert report.missing_tools == ()
    assert not report.tools_degraded


# --------------------------------------------------------------------------
# create_initialized_session
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_canonical_order(tmp_path: Path) -> None:
    session, resolved = healthy_setup(tmp_path)

    async def spawn(**kwargs: Any) -> dict[str, Any]:
        return {}

    async def resume(**kwargs: Any) -> dict[str, Any]:
        return {}

    initialized = await create_initialized_session(
        SessionRequest(
            resolved=resolved,
            approval_system="approvals",
            display_system="display",
            spawn_capability=spawn,
            resume_capability=resume,
        )
    )

    # session id minted
    assert initialized.session_id
    # metadata stamped into BOTH the mount plan and session.config
    assert resolved.mount_plan["root_session_id"] == initialized.session_id
    assert session.config["root_session_id"] == initialized.session_id
    assert session.config["application_host"] == APPLICATION_HOST
    # spawn/resume registered AFTER create_session
    assert session.coordinator.capabilities[SPAWN_CAPABILITY] is spawn
    assert session.coordinator.capabilities[RESUME_CAPABILITY] is resume
    # injected systems reached create_session
    prepared: Any = resolved.prepared
    assert prepared.create_kwargs["approval_system"] == "approvals"
    assert prepared.create_kwargs["display_system"] == "display"
    assert prepared.create_kwargs["is_resumed"] is False
    assert initialized.degraded_notice is None


@pytest.mark.asyncio
async def test_missing_provider_hard_fails_and_cleans_up(tmp_path: Path) -> None:
    mount_plan = {"providers": [{"module": "provider-anthropic"}]}
    coordinator = FakeCoordinator(providers={})  # nothing mounted
    session = FakeSession(coordinator)
    resolved = make_resolved(session, mount_plan, tmp_path)

    with pytest.raises(ProviderMountError) as excinfo:
        await create_initialized_session(SessionRequest(resolved=resolved))
    assert "anthropic" in str(excinfo.value)
    assert "doctor" in str(excinfo.value)
    assert session.cleaned  # session torn down on hard fail


@pytest.mark.asyncio
async def test_partial_provider_failure_degrades_not_fatal(tmp_path: Path) -> None:
    # One provider up (Anthropic), one down (a vLLM 'openmj' whose endpoint
    # is offline): the session runs on the working provider and notes the
    # other — it must NOT hard-fail (regression: tui killed the whole
    # app when any single provider failed to mount).
    mount_plan = {
        "providers": [
            {"module": "provider-anthropic"},
            {"module": "provider-vllm", "id": "openmj"},
        ]
    }
    coordinator = FakeCoordinator(providers={"anthropic": object()})
    session = FakeSession(coordinator)
    resolved = make_resolved(session, mount_plan, tmp_path)

    initialized = await create_initialized_session(SessionRequest(resolved=resolved))
    assert not session.cleaned  # session is live, not torn down
    assert initialized.degraded_notice is not None
    assert "openmj" in initialized.degraded_notice


@pytest.mark.asyncio
async def test_missing_tools_start_degraded_not_fatal(tmp_path: Path) -> None:
    mount_plan = {
        "providers": [{"module": "provider-anthropic"}],
        "tools": [{"module": "tool-filesystem"}],
    }
    coordinator = FakeCoordinator(providers={"anthropic": object()}, tools={})
    session = FakeSession(coordinator)
    resolved = make_resolved(session, mount_plan, tmp_path)

    initialized = await create_initialized_session(SessionRequest(resolved=resolved))
    assert initialized.degraded_notice is not None
    assert "tool-filesystem" in initialized.degraded_notice


@pytest.mark.asyncio
async def test_single_tool_module_failure_surfaces_by_name(tmp_path: Path) -> None:
    # RCA: tool-team-pulse fails to mount while the provider and tool-bash are
    # fine. The kernel swallows the mount error, so without reconciliation the
    # session limps on silently. It must start (not fatal — only zero providers
    # is fatal) AND name the failed module in the degraded notice.
    mount_plan = {
        "providers": [{"module": "provider-anthropic"}],
        "tools": [{"module": "tool-bash"}, {"module": "tool-team-pulse"}],
    }
    coordinator = FakeCoordinator(providers={"anthropic": object()}, tools={"bash": object()})
    session = FakeSession(coordinator)
    resolved = make_resolved(session, mount_plan, tmp_path)

    initialized = await create_initialized_session(SessionRequest(resolved=resolved))
    assert not session.cleaned  # session is live, not torn down
    assert initialized.degraded_notice is not None
    assert "tool-team-pulse" in initialized.degraded_notice
    assert "doctor" in initialized.degraded_notice


@pytest.mark.asyncio
async def test_resume_restores_transcript_preserving_system_prompt(
    tmp_path: Path,
) -> None:
    session, resolved = healthy_setup(tmp_path)
    context: FakeContext = session.coordinator.get("context")
    context.messages = [{"role": "system", "content": "fresh system prompt"}]

    transcript = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    initialized = await create_initialized_session(
        SessionRequest(resolved=resolved, session_id="resumed-id", initial_transcript=transcript)
    )

    assert initialized.session_id == "resumed-id"
    prepared: Any = resolved.prepared
    assert prepared.create_kwargs["is_resumed"] is True
    # transcript restored AND the fresh system prompt re-injected up front
    assert context.messages[0]["role"] == "system"
    assert context.messages[1:] == transcript


@pytest.mark.asyncio
async def test_cleanup_runs_unregister_handles_then_session(tmp_path: Path) -> None:
    session, resolved = healthy_setup(tmp_path)
    initialized = await create_initialized_session(SessionRequest(resolved=resolved))

    calls: list[str] = []
    initialized.unregister_handles.append(lambda: calls.append("first"))
    initialized.unregister_handles.append(lambda: calls.append("second"))

    await initialized.cleanup()
    assert calls == ["second", "first"]  # reverse order
    assert session.cleaned
    assert initialized.unregister_handles == []


@pytest.mark.asyncio
async def test_cleanup_awaits_async_unregister_handles(tmp_path: Path) -> None:
    session, resolved = healthy_setup(tmp_path)
    initialized = await create_initialized_session(SessionRequest(resolved=resolved))
    calls: list[str] = []

    async def async_unregister() -> None:
        calls.append("async")

    initialized.unregister_handles.append(async_unregister)

    await initialized.cleanup()

    assert calls == ["async"]
    assert session.cleaned
