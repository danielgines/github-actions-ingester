-- 0002_workflow_file_facts: what the workflow file declares besides its crons.
--
-- Filled by the same read that already collects `schedules`:
--   triggers            event names under `on` (push, workflow_dispatch, workflow_call ...)
--   reusable_workflows  `jobs.*.uses`: the reusable workflows this file calls
--   actions             `jobs.*.steps[].uses`: the actions this file runs

ALTER TABLE workflows ADD COLUMN IF NOT EXISTS triggers           TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE workflows ADD COLUMN IF NOT EXISTS reusable_workflows TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE workflows ADD COLUMN IF NOT EXISTS actions            TEXT[] NOT NULL DEFAULT '{}';

-- Re-read every file on the next cycle so the new columns fill right away
-- instead of waiting for the schedule refresh window.
UPDATE workflows SET schedules_synced_at = NULL;
