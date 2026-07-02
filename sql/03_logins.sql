-- ============================================================
-- 03_logins.sql — 4 SQL Server logins for the blocking agent
-- Run on each target OLTP server AND management/log servers
-- ============================================================

-- ── LOGIN 1: sql_agent_reader (VIEW SERVER STATE only) ────────
-- Used by: oltp_reader DBHub source
-- Permissions: DMV reads — sys.dm_exec_*, sys.dm_os_*, sys.dm_hadr_*, sys.dm_tran_*
-- Cannot: INSERT, UPDATE, DELETE, KILL, DDL

USE master;
GO
CREATE LOGIN sql_agent_reader WITH PASSWORD = 'ReplaceWithStrongPassword1!';
GO
GRANT VIEW SERVER STATE TO sql_agent_reader;
-- Verify: cannot issue KILL
-- EXECUTE AS LOGIN = 'sql_agent_reader'; KILL 1; -- Should fail


-- ── LOGIN 2: sql_agent_killer (ALTER ANY CONNECTION) ──────────
-- Used by: oltp_killer DBHub source
-- THIS IS THE ONLY LOGIN THAT CAN ISSUE KILL
-- Permissions: VIEW SERVER STATE + ALTER ANY CONNECTION + ALTER DATABASE (for QS plan forcing)

USE master;
GO
CREATE LOGIN sql_agent_killer WITH PASSWORD = 'ReplaceWithStrongPassword2!';
GO
GRANT VIEW SERVER STATE TO sql_agent_killer;
GRANT ALTER ANY CONNECTION TO sql_agent_killer;
-- For Query Store plan forcing (sp_query_store_force_plan requires ALTER DATABASE):
-- GRANT ALTER ANY DATABASE TO sql_agent_killer;  -- Or grant per-database below
-- Verify: only login with KILL permission
-- SELECT l.name, p.permission_name FROM sys.server_principals l
-- JOIN sys.server_permissions p ON l.principal_id = p.grantee_principal_id
-- WHERE l.name = 'sql_agent_killer';


-- ── LOGIN 3: sql_agent_config (SELECT on AgentConfigDB) ───────
-- Used by: config_reader DBHub source
-- Target server: management server with AgentConfigDB

USE master;
GO
CREATE LOGIN sql_agent_config WITH PASSWORD = 'ReplaceWithStrongPassword3!';
GO

USE AgentConfigDB;
GO
CREATE USER sql_agent_config FOR LOGIN sql_agent_config;
GO
GRANT SELECT ON SCHEMA::dbo TO sql_agent_config;
-- No INSERT/UPDATE/DELETE on config tables


-- ── LOGIN 4: sql_agent_logger (INSERT on AgentLogDB) ──────────
-- Used by: log_writer DBHub source
-- Target server: logging server with AgentLogDB
-- INSERT only — immutable trigger prevents UPDATE/DELETE on KillAuditLog

USE master;
GO
CREATE LOGIN sql_agent_logger WITH PASSWORD = 'ReplaceWithStrongPassword4!';
GO

USE AgentLogDB;
GO
CREATE USER sql_agent_logger FOR LOGIN sql_agent_logger;
GO
GRANT INSERT ON dbo.KillAuditLog   TO sql_agent_logger;
GRANT INSERT ON dbo.BlockingEventLog TO sql_agent_logger;
GRANT INSERT ON dbo.RCASnapshotLog  TO sql_agent_logger;
-- No UPDATE/DELETE — immutable log design
