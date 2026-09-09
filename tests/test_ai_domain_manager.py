import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.xray.ai_routing import (
    artifact,
    candidates,
    classifier,
    common,
    manager,
    observations,
    repository,
    selector,
)
from app.xray.operation_lock import LockBusyError, exclusive_file_lock


class _FakeHttpResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class AiDomainManagerTest(unittest.TestCase):
    def test_canonical_classifier_exports_split_helpers(self):
        for name in (
            "build_codex_command",
            "extract_output_text",
            "matches_domain_suffixes",
            "sync_codex_home",
            "validate_classification_results",
        ):
            self.assertTrue(callable(getattr(classifier, name, None)), name)

    @mock.patch.object(artifact.subprocess, "run")
    def test_rerender_config_passes_panel_ports_file_next_to_config(self, mocked_run):
        mocked_run.return_value = mock.Mock(returncode=0, stderr="", stdout="")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifact.rerender_config(
                "app.xray.render_config",
                root / "xray.env",
                root / "runtime" / "config.json",
                root / "runtime" / "client-test.json",
                root / "runtime" / "client-share.txt",
                root / "runtime" / "dynamic-routing.json",
            )

        command = mocked_run.call_args.args[0]
        self.assertEqual(
            command[command.index("--panel-ports-file") + 1],
            str(root / "runtime" / "panel-ports.json"),
        )

    def test_sqlite_lock_retry_retries_transient_lock(self):
        attempts = []

        def operation():
            attempts.append(True)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with mock.patch.object(common.time, "sleep") as sleep:
            result = common.run_with_sqlite_lock_retry(operation)

        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_save_ai_domains_reports_locked_database_after_retries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "panel.db"
            db_path.touch()
            with mock.patch.object(
                repository,
                "_save_ai_domains_to_panel_db_once",
                side_effect=sqlite3.OperationalError("database is locked"),
            ), mock.patch.object(common.time, "sleep"):
                status = repository.save_ai_domains_to_panel_db(
                    db_path,
                    {"domains": []},
                    {"domains": {}},
                )

        self.assertEqual(status["status"], "skipped")
        self.assertEqual(status["reason"], "database_locked")

    def test_save_ai_domains_persists_observed_ai_and_omits_unobserved_builtins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "panel.db"
            db_path.touch()
            report = {
                "generated_at": "2026-08-31T12:00:00+00:00",
                "window_start": "2026-08-31T11:00:00+00:00",
                "window_end": "2026-08-31T12:00:00+00:00",
                "domains": [
                    {
                        "domain": "api.openai.com",
                        "hits": 3,
                        "classification": "ai",
                        "reason": "AI API",
                        "protocols": ["tcp"],
                        "first_seen": "2026-08-31T11:10:00+00:00",
                        "last_seen": "2026-08-31T11:50:00+00:00",
                    }
                ],
            }
            decisions = {
                "domains": {
                    "api.openai.com": {
                        "classification": "ai",
                        "reason": "AI API",
                        "source": "builtin",
                        "model": "test",
                    },
                    "chatgpt.com": {
                        "classification": "ai",
                        "reason": "known AI",
                        "source": "builtin",
                        "model": "test",
                    },
                }
            }

            status = repository.save_ai_domains_to_panel_db(db_path, report, decisions)

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT domain, total_hits FROM ai_domains ORDER BY domain"
                ).fetchall()
                observations = conn.execute(
                    "SELECT domain, hits FROM ai_domain_observations"
                ).fetchall()

        self.assertEqual(status["status"], "written")
        self.assertEqual(rows, [("api.openai.com", 3)])
        self.assertEqual(observations, [("api.openai.com", 3)])

    def test_sync_log_reads_recent_remote_access_log(self):
        controller = mock.Mock()
        controller.supports_logs.return_value = True
        controller.read_access_log_delta.return_value = {
            "exists": True,
            "inode": "remote-inode",
            "offset": 256,
            "data": (
                "2026/08/31 11:30:00.000000 from 127.0.0.1:1234 "
                "accepted tcp:api.openai.com:443 [panel-31098 -> ai_proxy]\n"
            ),
        }
        state = {"log_inode": "", "log_offset": 0, "events": []}
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

        observations.sync_log(
            Path("/does/not/exist"),
            state,
            data_plane_controller=controller,
            lookback_seconds=3600,
            now=now,
        )

        controller.read_access_log_delta.assert_called_once_with(
            "",
            0,
            since_epoch=(now - timedelta(hours=1)).timestamp(),
        )
        self.assertEqual(state["log_inode"], "remote-inode")
        self.assertEqual(state["log_offset"], 256)
        self.assertEqual(state["events"][0]["domain"], "api.openai.com")

    def test_domain_report_records_classifier_and_effective_traffic_route(self):
        observed_at = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        state = {
            "events": [
                {"domain": "chatgpt.com", "protocol": "tcp", "seen_at": observed_at},
                {"domain": "example.com", "protocol": "tcp", "seen_at": observed_at},
            ]
        }
        decisions = {
            "domains": {
                "chatgpt.com": {
                    "classification": "ai",
                    "reason": "known AI",
                    "source": "codex",
                    "model": "gpt-5.5",
                },
                "example.com": {
                    "classification": "not_ai",
                    "reason": "ordinary site",
                    "source": "codex",
                    "model": "gpt-5.5",
                },
            }
        }

        report = artifact.build_domain_report(
            state,
            observed_at - timedelta(hours=1),
            observed_at,
            decisions,
            {"upstream_host": "nat.qq.pw", "upstream_port": 27166},
            None,
            {"status": "applied", "reason": ""},
        )
        domains = {item["domain"]: item for item in report["domains"]}

        self.assertEqual(domains["chatgpt.com"]["source"], "codex")
        self.assertEqual(domains["chatgpt.com"]["model"], "gpt-5.5")
        self.assertEqual(domains["chatgpt.com"]["traffic_route"]["outbound_tag"], "ai_proxy")
        self.assertEqual(domains["chatgpt.com"]["traffic_route"]["target"]["upstream_host"], "nat.qq.pw")
        self.assertEqual(domains["example.com"]["traffic_route"]["outbound_tag"], "direct")

    def test_forced_fallback_report_has_no_fake_upstream_and_writes_text(self):
        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        report = artifact.build_domain_report(
            {"events": []},
            now - timedelta(hours=1),
            now,
            {"domains": {}},
            {
                "probe_status": "manual_fallback",
                "failure_reason": "manual_override",
                "is_reachable": False,
                "candidates": [],
            },
            None,
            {"status": "manual_fallback", "reason": "manual_override"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            artifact.write_domain_report(output_dir, report)
            saved_report = json.loads((output_dir / "latest.json").read_text(encoding="utf-8"))
            saved_text = (output_dir / "latest.txt").read_text(encoding="utf-8")

        self.assertNotIn("upstream_host", saved_report["ai_target"])
        self.assertIn("ai_target: unavailable", saved_text)

    def test_read_ai_routing_manual_mode_defaults_and_reads_persisted_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "panel.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute(
                    "INSERT INTO app_state (key, value) VALUES (?, ?)",
                    ("ai_routing_manual_mode", "forced_fallback"),
                )
                conn.commit()

            self.assertEqual(repository.read_ai_routing_manual_mode(db_path), "forced_fallback")
            self.assertEqual(repository.read_ai_routing_manual_mode(Path(tmpdir) / "missing.db"), "auto")

    def test_build_data_plane_controller_uses_remote_command_timeout(self):
        args = mock.Mock(
            ai_upstream_candidates=[{"upstream_host": "primary.example.com", "upstream_port": 27166}],
            data_plane_access_log_path="",
            data_plane_ssh_target="root@example.com",
            data_plane_config_path="/root/xray/runtime/config.json",
            data_plane_api_server="127.0.0.1:10085",
            data_plane_xray_bin="/usr/local/bin/xray",
            data_plane_local_bin="",
            data_plane_docker_bin="docker",
            data_plane_container_name="xray",
            data_plane_restart_command="",
            data_plane_ssh_bin="ssh",
            data_plane_ssh_options=(),
            data_plane_ssh_known_hosts_file="/root/.ssh/known_hosts",
            data_plane_remote_command_timeout=30.0,
            data_plane_dynamic_routing_path="/root/xray/runtime/dynamic-routing.json",
            config_out=Path("/tmp/config.json"),
            dynamic_routing_path=Path("/tmp/dynamic-routing.json"),
        )

        controller = manager.build_data_plane_controller(args)

        self.assertEqual(controller.config.remote_command_timeout, 30.0)
        self.assertEqual(controller.config.panel_ports_path, "/root/xray/runtime/panel-ports.json")
        self.assertEqual(controller.config.source_panel_ports_path, Path("/tmp/panel-ports.json"))

    def test_run_once_restarts_when_remote_config_was_replaced(self):
        self._check_run_once_recovery()

    def test_run_once_retries_failed_restart_with_unchanged_files(self):
        for failure in (False, RuntimeError("synthetic restart timeout")):
            with self.subTest(failure=type(failure).__name__):
                self._check_run_once_recovery(failure=failure)

    def test_forced_fallback_renders_without_ai_fragment(self):
        self._check_run_once_recovery(forced=True)

    def test_forced_fallback_skips_domain_classifier(self):
        with mock.patch.object(
            manager,
            "classify_pending_domains",
            side_effect=AssertionError("forced fallback must not classify domains"),
        ):
            self._check_run_once_recovery(forced=True)

    def test_run_once_honors_explicit_manual_mode_override(self):
        self._check_run_once_recovery(forced=True, manual_mode_override="forced_fallback")

    def test_run_once_reports_success_when_post_apply_persistence_fails(self):
        result = self._check_run_once_recovery(report_failure=True)
        self.assertEqual(result["status"], "applied_with_reporting_error")

    def test_run_once_rejects_a_concurrent_manager_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            lock_path = root / "runtime" / ".ai-domain-manager.lock"
            lock_path.parent.mkdir()
            args = mock.Mock(config_out=root / "runtime" / "config.json", apply_lock_path=lock_path)
            with exclusive_file_lock(lock_path), self.assertRaises(LockBusyError):
                manager.run_once(args)

    def test_run_once_delegates_unmanaged_data_plane_reload(self):
        controller = mock.Mock()
        controller.is_configured.return_value = True
        controller.mode = "unmanaged"
        controller.supports_sync.return_value = False

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_out = root / "runtime" / "config.json"
            config_out.parent.mkdir()
            config_out.write_text("old", encoding="utf-8")
            dynamic_routing_path = root / "runtime" / "dynamic-routing.json"
            args = mock.Mock(
                log_state_path=root / "log-state.json",
                log_path=root / "access.log",
                lookback_seconds=3600,
                classification_state_path=root / "decisions.json",
                panel_db_path=root / "panel.db",
                panel_route_listen_port=None,
                ai_upstream_candidates=[{"upstream_host": "ai.example.com", "upstream_port": 27166}],
                ai_upstream_probe_timeout_seconds=2.0,
                batch_size=50,
                codex_classifier_enabled=False,
                openai_classifier_enabled=False,
                proxy_template_path=root / "missing-template.json",
                dynamic_routing_path=dynamic_routing_path,
                render_script="app.xray.render_config",
                env_file=root / "xray.env",
                config_out=config_out,
                client_out=root / "runtime" / "client-test.json",
                share_out=root / "runtime" / "client-share.txt",
                data_plane_config_path="/etc/xray/config.json",
                data_plane_external_reloader_enabled=True,
                restart_command="",
                restart_container_name="",
                docker_timeout_seconds=5,
                report_output_dir=root / "reports",
            )

            def render(*_args):
                config_out.write_text("new", encoding="utf-8")

            with mock.patch.object(manager, "build_data_plane_controller", return_value=controller), \
                mock.patch.object(manager, "sync_log"), \
                mock.patch.object(manager, "sync_builtin_domain_decisions"), \
                mock.patch.object(manager, "read_ai_routing_manual_mode", return_value="auto"), \
                mock.patch.object(
                    manager,
                    "select_ai_target",
                    return_value={"probe_status": "all_reachable", "is_reachable": True, "candidates": []},
                ), \
                mock.patch.object(manager, "rerender_config", side_effect=render), \
                mock.patch.object(manager, "save_ai_domains_to_panel_db", return_value={}), \
                mock.patch.object(manager, "write_domain_report"), \
                mock.patch.object(manager, "save_log_state"), \
                mock.patch.object(manager, "save_json"):
                result = manager.run_once(args)
                self.assertTrue((root / "runtime" / "config.json.pending-apply").exists())

        self.assertEqual(result["config_apply_status"], "delegated")
        controller.restart.assert_not_called()

    def test_run_once_does_not_signal_external_reloader_before_failed_render(self):
        controller = mock.Mock()
        controller.is_configured.return_value = True
        controller.mode = "local"
        controller.supports_sync.return_value = False

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_out = root / "runtime" / "config.json"
            config_out.parent.mkdir()
            config_out.write_text("old", encoding="utf-8")
            args = mock.Mock(
                log_state_path=root / "log-state.json",
                log_path=root / "access.log",
                lookback_seconds=3600,
                classification_state_path=root / "decisions.json",
                panel_db_path=root / "panel.db",
                panel_route_listen_port=None,
                ai_upstream_candidates=[{"upstream_host": "ai.example.com", "upstream_port": 27166}],
                ai_upstream_probe_timeout_seconds=2.0,
                batch_size=50,
                codex_classifier_enabled=False,
                openai_classifier_enabled=False,
                proxy_template_path=root / "missing-template.json",
                dynamic_routing_path=root / "runtime" / "dynamic-routing.json",
                config_out=config_out,
                client_out=root / "runtime" / "client-test.json",
                share_out=root / "runtime" / "client-share.txt",
                data_plane_config_path=str(config_out),
                data_plane_external_reloader_enabled=True,
                restart_command="",
                restart_container_name="",
                docker_timeout_seconds=5,
                report_output_dir=root / "reports",
            )

            with mock.patch.object(manager, "build_data_plane_controller", return_value=controller), \
                mock.patch.object(manager, "sync_log"), \
                mock.patch.object(manager, "sync_builtin_domain_decisions"), \
                mock.patch.object(manager, "read_ai_routing_manual_mode", return_value="auto"), \
                mock.patch.object(
                    manager,
                    "select_ai_target",
                    return_value={"probe_status": "all_reachable", "is_reachable": True, "candidates": []},
                ), \
                mock.patch.object(manager, "rerender_config", side_effect=RuntimeError("render failed")), \
                self.assertRaisesRegex(RuntimeError, "render failed"):
                manager.run_once(args)

            self.assertFalse(config_out.with_name("config.json.pending-apply").exists())

    def test_run_once_reports_unmanaged_data_plane_without_attempting_restart(self):
        controller = mock.Mock()
        controller.is_configured.return_value = True
        controller.mode = "unmanaged"
        controller.supports_sync.return_value = False

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_out = root / "runtime" / "config.json"
            config_out.parent.mkdir()
            config_out.write_text("old", encoding="utf-8")
            dynamic_routing_path = root / "runtime" / "dynamic-routing.json"
            args = mock.Mock(
                log_state_path=root / "log-state.json",
                log_path=root / "access.log",
                lookback_seconds=3600,
                classification_state_path=root / "decisions.json",
                panel_db_path=root / "panel.db",
                panel_route_listen_port=None,
                ai_upstream_candidates=[{"upstream_host": "ai.example.com", "upstream_port": 27166}],
                ai_upstream_probe_timeout_seconds=2.0,
                batch_size=50,
                codex_classifier_enabled=False,
                openai_classifier_enabled=False,
                proxy_template_path=root / "missing-template.json",
                dynamic_routing_path=dynamic_routing_path,
                render_script="app.xray.render_config",
                env_file=root / "xray.env",
                config_out=config_out,
                client_out=root / "runtime" / "client-test.json",
                share_out=root / "runtime" / "client-share.txt",
                data_plane_config_path="/etc/xray/config.json",
                data_plane_external_reloader_enabled=False,
                restart_command="",
                restart_container_name="",
                docker_timeout_seconds=5,
                report_output_dir=root / "reports",
            )

            def render(*_args):
                config_out.write_text("new", encoding="utf-8")

            with mock.patch.object(manager, "build_data_plane_controller", return_value=controller), \
                mock.patch.object(manager, "sync_log"), \
                mock.patch.object(manager, "sync_builtin_domain_decisions"), \
                mock.patch.object(manager, "read_ai_routing_manual_mode", return_value="auto"), \
                mock.patch.object(
                    manager,
                    "select_ai_target",
                    return_value={"probe_status": "all_reachable", "is_reachable": True, "candidates": []},
                ), \
                mock.patch.object(manager, "rerender_config", side_effect=render), \
                mock.patch.object(manager, "save_ai_domains_to_panel_db", return_value={}), \
                mock.patch.object(manager, "write_domain_report"), \
                mock.patch.object(manager, "save_log_state"), \
                mock.patch.object(manager, "save_json"):
                result = manager.run_once(args)

        self.assertEqual(result["config_apply_status"], "unmanaged")
        controller.restart.assert_not_called()

    def _check_run_once_recovery(
        self,
        failure=None,
        forced=False,
        report_failure=False,
        manual_mode_override=None,
    ):
        controller = mock.Mock()
        controller.is_configured.return_value = True
        controller.supports_sync.return_value = True
        controller.sync_generated_files.return_value = ["/root/xray/runtime/config.json"]
        controller.supports_restart.return_value = True
        controller.restart.return_value = True
        if failure is not None:
            controller.restart.side_effect = [failure, True]
            controller.sync_generated_files.side_effect = [["/root/xray/runtime/config.json"], [], []]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_out = root / "runtime" / "config.json"
            config_out.parent.mkdir()
            config_out.write_text("same", encoding="utf-8")
            dynamic_routing_path = root / "runtime" / "dynamic-routing.json"
            if forced:
                dynamic_routing_path.write_text('{"routing": {"rules": []}}', encoding="utf-8")

            def render(*_args):
                if forced:
                    self.assertFalse(dynamic_routing_path.exists())
                config_out.write_text("direct" if forced else "same", encoding="utf-8")
            args = mock.Mock(
                log_state_path=root / "log-state.json",
                log_path=root / "access.log",
                lookback_seconds=3600,
                classification_state_path=root / "decisions.json",
                panel_db_path=root / "panel.db",
                panel_route_listen_port=None,
                ai_upstream_candidates=[{"upstream_host": "ai.example.com", "upstream_port": 27166}],
                ai_upstream_probe_timeout_seconds=2.0,
                batch_size=50,
                codex_classifier_enabled=False,
                openai_classifier_enabled=False,
                proxy_template_path=root / "missing-template.json",
                dynamic_routing_path=dynamic_routing_path,
                render_script="app.xray.render_config",
                env_file=root / "xray.env",
                config_out=config_out,
                client_out=root / "runtime" / "client-test.json",
                share_out=root / "runtime" / "client-share.txt",
                data_plane_config_path="/root/xray/runtime/config.json",
                restart_command="",
                restart_container_name="",
                docker_timeout_seconds=5,
                report_output_dir=root / "reports",
                manual_mode=manual_mode_override,
            )

            with mock.patch.object(manager, "build_data_plane_controller", return_value=controller), \
                mock.patch.object(manager, "sync_log"), \
                mock.patch.object(manager, "sync_builtin_domain_decisions"), \
                mock.patch.object(
                    manager,
                    "read_ai_routing_manual_mode",
                    side_effect=AssertionError("explicit mode should not read panel state")
                    if manual_mode_override is not None
                    else None,
                    return_value=("forced_fallback" if forced else "auto")
                    if manual_mode_override is None
                    else mock.DEFAULT,
                ), \
                mock.patch.object(
                    manager,
                    "select_ai_target",
                    return_value={
                        "probe_status": "all_reachable",
                        "is_reachable": True,
                        "upstream_host": "ai.example.com",
                        "upstream_port": 27166,
                        "candidates": [],
                    },
                ), \
                mock.patch.object(manager, "rerender_config", side_effect=render), \
                mock.patch.object(
                    manager,
                    "save_ai_domains_to_panel_db",
                    side_effect=RuntimeError("synthetic report failure") if report_failure else None,
                    return_value={} if not report_failure else mock.DEFAULT,
                ), \
                mock.patch.object(manager, "write_domain_report"), \
                mock.patch.object(manager, "save_log_state"):
                pending = config_out.with_name("config.json.pending-apply")
                if failure is not None:
                    with self.assertRaises(RuntimeError):
                        manager.run_once(args)
                    self.assertTrue(pending.exists())
                result = manager.run_once(args)
                self.assertFalse(pending.exists())
                if failure is not None:
                    manager.run_once(args)
                if forced:
                    self.assertEqual(config_out.read_text(encoding="utf-8"), "direct")

        self.assertEqual(controller.restart.call_count, 2 if failure is not None else 1)
        return result

    @mock.patch.object(selector, "probe_ai_upstream_candidate")
    def test_select_ai_target_can_manually_select_backup(self, mocked_probe):
        mocked_probe.side_effect = [
            {
                "upstream_host": "primary.example.com",
                "upstream_port": 27166,
                "is_reachable": True,
                "failure_reason": "",
                "checked_at": "2026-08-23T00:00:00+00:00",
            },
            {
                "upstream_host": "backup.example.com",
                "upstream_port": 27166,
                "is_reachable": True,
                "failure_reason": "",
                "checked_at": "2026-08-23T00:00:00+00:00",
            },
        ]

        result = selector.select_ai_target(
            [
                {"upstream_host": "primary.example.com", "upstream_port": 27166},
                {"upstream_host": "backup.example.com", "upstream_port": 27166},
            ],
            2.0,
            preferred_index=1,
        )

        self.assertEqual(result["selected_index"], 1)
        self.assertEqual(result["upstream_host"], "backup.example.com")
        self.assertEqual(result["probe_status"], "manual_selected")
        self.assertEqual(result["selection_mode"], "manual")

    @mock.patch.object(selector, "probe_ai_upstream_candidate")
    def test_select_ai_target_reports_manual_unreachable_for_unreachable_selection(self, mocked_probe):
        mocked_probe.side_effect = [
            {
                "upstream_host": "primary.example.com",
                "upstream_port": 27166,
                "is_reachable": True,
                "failure_reason": "",
                "checked_at": "2026-08-23T00:00:00+00:00",
            },
            {
                "upstream_host": "backup.example.com",
                "upstream_port": 27166,
                "is_reachable": False,
                "failure_reason": "timed out",
                "checked_at": "2026-08-23T00:00:00+00:00",
            },
        ]

        result = selector.select_ai_target(
            [
                {"upstream_host": "primary.example.com", "upstream_port": 27166},
                {"upstream_host": "backup.example.com", "upstream_port": 27166},
            ],
            2.0,
            preferred_index=1,
        )

        self.assertEqual(result["selected_index"], 1)
        self.assertEqual(result["probe_status"], "manual_unreachable")
        self.assertEqual(result["failure_reason"], "timed out")

    def test_gemini_session_domains_are_forced_to_ai_route(self):
        for domain in (
            "gemini.google.com",
            "chat.gemini.google.com",
            "accounts.google.com",
            "generativelanguage.googleapis.com",
            "scholar.google.com",
        ):
            self.assertTrue(classifier.matches_forced_ai_route_domain(domain))

    def test_chatgpt_and_claude_domain_families_are_forced_to_ai_route(self):
        for domain in (
            "chatgpt.com",
            "chat.openai.com",
            "api.openai.com",
            "cdn.oaistatic.com",
            "files.oaiusercontent.com",
            "claude.ai",
            "console.anthropic.com",
            "assets.claudeusercontent.com",
        ):
            self.assertTrue(classifier.matches_forced_ai_route_domain(domain))

    def test_aws_domain_families_are_forced_to_ai_route(self):
        for domain in (
            "s3.amazonaws.com",
            "cognito-identity.us-west-2.amazonaws.com",
            "s3.cn-north-1.amazonaws.com.cn",
            "s3.cn-north-1.api.amazonwebservices.com.cn",
            "checkip.global.api.aws",
            "my-function.lambda-url.us-east-1.on.aws",
            "console.aws.amazon.com",
            "signin.aws.amazon.com",
            "directory.awsapps.com",
            "login.awsapps.cn",
            "cdn.awsstatic.com",
            "cdn.us-east-1.prod.moon.dubai.aws.dev",
            "redirect.prod.experiment.routing.cloudfront.aws.a2z.com",
            "foo.aws",
        ):
            self.assertTrue(classifier.matches_forced_ai_route_domain(domain))

    def test_aws_shared_domain_families_are_not_overmatched(self):
        for domain in (
            "www.amazon.com",
            "cdn.example.cloudfront.net",
            "video.example.live-video.net",
            "service.a2z.com",
        ):
            self.assertFalse(classifier.matches_forced_ai_route_domain(domain))

    def test_default_ai_redirect_uses_ipv4(self):
        payload, reason = artifact.render_proxy_template(
            Path("/does/not/exist"),
            {
                "upstream_host": "nat.qq.pw",
                "upstream_port": 27166,
            },
            None,
        )

        self.assertEqual(reason, "builtin_freedom_redirect")
        self.assertEqual(payload["outbounds"][0]["settings"]["domainStrategy"], "UseIPv4")

    def test_resolve_openai_endpoint_defaults_to_responses_for_remote(self):
        endpoint, api_style = classifier.resolve_openai_endpoint("https://api.openai.com")
        self.assertEqual(endpoint, "https://api.openai.com/v1/responses")
        self.assertEqual(api_style, "responses")

    def test_resolve_openai_endpoint_defaults_to_chat_completions_for_local(self):
        endpoint, api_style = classifier.resolve_openai_endpoint("http://127.0.0.1:11434/v1")
        self.assertEqual(endpoint, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(api_style, "chat_completions")

    def test_local_openai_base_url_detection(self):
        self.assertTrue(classifier.is_local_openai_base_url("http://127.0.0.1:11434/v1"))
        self.assertTrue(classifier.is_local_openai_base_url("http://192.168.1.10:8000"))
        self.assertFalse(classifier.is_local_openai_base_url("https://api.openai.com/v1/responses"))

    @mock.patch("urllib.request.urlopen")
    def test_classify_domains_via_chat_completions_without_api_key(self, mocked_urlopen):
        mocked_urlopen.return_value = _FakeHttpResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                [
                                    {
                                        "domain": "chatgpt.com",
                                        "classification": "ai",
                                        "reason": "AI chat product",
                                    },
                                    {
                                        "domain": "example.com",
                                        "classification": "not_ai",
                                        "reason": "general website",
                                    },
                                ]
                            )
                        }
                    }
                ]
            }
        )

        result = classifier.classify_domains_via_openai(
            ["chatgpt.com", "example.com"],
            api_key="",
            model="qwen2.5",
            base_url="http://127.0.0.1:11434/v1",
            timeout_seconds=5,
            allow_no_key=True,
        )

        self.assertEqual(result["chatgpt.com"]["classification"], "ai")
        self.assertEqual(result["example.com"]["classification"], "not_ai")
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertIsNone(request.get_header("Authorization"))
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["role"], "user")

    @mock.patch("urllib.request.urlopen")
    def test_classify_domains_via_responses_keeps_authorization_header(self, mocked_urlopen):
        mocked_urlopen.return_value = _FakeHttpResponse(
            {
                "output_text": json.dumps(
                    [
                        {
                            "domain": "openai.com",
                            "classification": "ai",
                            "reason": "AI model provider",
                        }
                    ]
                )
            }
        )

        result = classifier.classify_domains_via_openai(
            ["openai.com"],
            api_key="secret-key",
            model="gpt-5.5",
            base_url="https://api.openai.com",
            timeout_seconds=5,
        )

        self.assertEqual(result["openai.com"]["classification"], "ai")
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIn("input", payload)

    @mock.patch("app.xray.ai_routing.classifier.classify_domains_via_openai")
    def test_classify_pending_domains_keeps_pending_when_openai_unavailable(self, mocked_openai):
        mocked_openai.side_effect = RuntimeError("openai http 401")
        decisions = {"domains": {}}
        observed_domains = {"unknown.example"}
        with tempfile.TemporaryDirectory() as tmpdir:
            decisions_path = f"{tmpdir}/ai-domain-decisions.json"
            args = mock.Mock(
                batch_size=50,
                codex_classifier_enabled=False,
                openai_classifier_enabled=True,
                openai_api_key="bad-key",
                openai_model="openai/gpt-5-nano",
                openai_base_url="https://openrouter.ai/api/v1/chat/completions",
                openai_timeout_seconds=45,
                openai_allow_no_key=False,
            )

            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                pending = classifier.classify_pending_domains(
                    decisions,
                    decisions_path,
                    observed_domains,
                    args,
                )

        self.assertEqual(pending, ["unknown.example"])
        self.assertEqual(decisions["domains"], {})
        self.assertIn("openai classifier unavailable", stderr.getvalue())

    def test_probe_uses_reality_callback_when_candidate_has_sni(self):
        controller = mock.Mock()
        controller.probe_reality_endpoint.return_value = {
            "ok": False,
            "error": "TLS handshake failed",
            "method": "reality",
        }

        result = candidates.probe_ai_upstream_candidate(
            {
                "upstream_host": "ai.example.com",
                "upstream_port": 443,
                "probe_server_name": "www.example.com",
            },
            2.0,
            probe_controller=controller,
        )

        self.assertFalse(result["is_reachable"])
        self.assertEqual(result["probe_method"], "reality")
        controller.probe_reality_endpoint.assert_called_once_with(
            "ai.example.com",
            443,
            "www.example.com",
            2.0,
        )

    def test_select_ai_target_does_not_call_all_unreachable_on_probe_management_error(self):
        controller = mock.Mock()
        controller.probe_tcp_endpoint.return_value = {
            "ok": False,
            "error": "ssh authentication failed",
            "management_error": True,
            "method": "tcp",
        }

        result = selector.select_ai_target(
            [{"upstream_host": "ai.example.com", "upstream_port": 443}],
            2.0,
            probe_controller=controller,
        )

        self.assertEqual(result["probe_status"], "probe_error")
        self.assertFalse(selector.should_fallback_to_primary_route(result))

    @mock.patch("app.xray.ai_routing.manager.build_data_plane_controller")
    @mock.patch("app.xray.ai_routing.manager.rerender_config")
    @mock.patch("app.xray.ai_routing.selector.probe_ai_upstream_candidate")
    def test_run_once_falls_back_to_primary_route_when_all_ai_upstreams_are_unreachable(
        self,
        mocked_probe,
        mocked_rerender,
        mocked_controller_builder,
    ):
        mocked_probe.return_value = {
            "upstream_host": "ai.example.com",
            "upstream_port": 443,
            "candidate_type": "template",
            "is_reachable": False,
            "failure_reason": "timed out",
            "checked_at": "2026-06-22T00:00:00+00:00",
        }
        mocked_rerender.side_effect = (
            lambda _render_script, _env_file, config_out, _client_out, _share_out, _dynamic_routing_file:
            config_out.write_text("{}", encoding="utf-8")
        )
        mocked_controller_builder.return_value = mock.Mock(
            is_configured=mock.Mock(return_value=False),
            supports_restart=mock.Mock(return_value=False),
            supports_logs=mock.Mock(return_value=False),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            log_path = root / "access.log"
            log_state_path = root / "log-state.json"
            decisions_path = root / "ai-domain-decisions.json"
            dynamic_routing_path = root / "dynamic-routing.json"
            config_out = root / "config.json"
            client_out = root / "client.json"
            share_out = root / "share.txt"
            report_output_dir = root / "reports"

            decisions_path.write_text(
                json.dumps(
                    {
                        "domains": {
                            "openai.com": {
                                "classification": "ai",
                                "reason": "known ai",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            dynamic_routing_path.write_text('{"stale": true}', encoding="utf-8")

            args = mock.Mock(
                log_state_path=log_state_path,
                log_path=log_path,
                lookback_seconds=3600,
                classification_state_path=decisions_path,
                ai_upstream_candidates=[
                    {
                        "upstream_host": "ai.example.com",
                        "upstream_port": 443,
                        "candidate_type": "template",
                    }
                ],
                ai_upstream_probe_timeout_seconds=2.0,
                panel_db_path=root / "panel.db",
                panel_route_listen_port=None,
                batch_size=50,
                codex_classifier_enabled=False,
                openai_classifier_enabled=False,
                proxy_template_path=root / "missing-ai-proxy-outbound.json",
                dynamic_routing_path=dynamic_routing_path,
                render_script="app.xray.render_config",
                env_file=root / "xray.env",
                config_out=config_out,
                client_out=client_out,
                share_out=share_out,
                restart_command="",
                restart_container_name="",
                docker_timeout_seconds=5,
                report_output_dir=report_output_dir,
            )

            manager.run_once(args)

            self.assertFalse(dynamic_routing_path.exists())
            report = json.loads((report_output_dir / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(report["route_status"]["status"], "fallback_to_primary")
            self.assertEqual(report["route_status"]["reason"], "ai_upstream_unreachable")
            self.assertEqual(report["ai_target"]["probe_status"], "all_unreachable")


if __name__ == "__main__":
    unittest.main()
