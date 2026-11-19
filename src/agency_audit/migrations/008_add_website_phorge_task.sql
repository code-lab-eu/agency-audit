-- 008_add_website_phorge_task.sql — The Phorge task for scraping a website
--
-- Set when the dashboard creates the task or finds the one existing task for
-- the website; NULL until then.

ALTER TABLE websites
    ADD COLUMN phorge_task_id INTEGER,
    ADD COLUMN phorge_task_phid TEXT;
