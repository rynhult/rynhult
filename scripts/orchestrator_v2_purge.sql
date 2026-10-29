-- Delete what the Orchestrator v2 preview stored (Guide: Orchestrator, "Data it keeps").
-- Run only with the preview off and Rynhult stopped, after a backup. SQLite and PostgreSQL alike.
-- It keeps teams, sessions, their conversations and branches, the audit log, and open Inbox items.
BEGIN;
DELETE FROM work_item;
DELETE FROM orchestration_run;
DELETE FROM plan_version;
DELETE FROM plan_phase;
DELETE FROM orchestration_plan;
DELETE FROM task_report;
DELETE FROM message_delivery;
DELETE FROM selection;
DELETE FROM reservation;
DELETE FROM gate_job;
DELETE FROM setting WHERE key IN ('orchestrator.selection_rules', 'orchestrator.host_budget', 'orchestrator.gate_commands');
DELETE FROM attention_item WHERE dedupe_key LIKE 'worklist:%' AND resolved_at IS NOT NULL;
COMMIT;
