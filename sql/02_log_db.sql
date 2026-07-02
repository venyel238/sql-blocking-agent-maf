-- ============================================================
-- 02_log_db.sql — AgentLogDB DDL + immutable triggers
-- Run on the logging SQL Server instance
-- ============================================================

CREATE DATABASE AgentLogDB;
GO
USE AgentLogDB;
GO

CREATE TABLE dbo.KillAuditLog (
    KillID                  UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID() PRIMARY KEY,
    KillTimeUTC             DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME(),
    ServerName              NVARCHAR(100)    NOT NULL,
    KilledSPID              INT              NOT NULL,
    KilledLogin             NVARCHAR(128)    NOT NULL,
    KilledProgram           NVARCHAR(256)    NULL,
    KilledHost              NVARCHAR(128)    NULL,
    KillReason              NVARCHAR(MAX)    NULL,
    RiskLevel               NVARCHAR(20)     NULL,
    WaitTimeAtKillMs        BIGINT           NULL,
    -- Rollback safety analysis
    LogUsedMB               DECIMAL(18,3)    NULL,
    RollbackSafetyRating    NVARCHAR(60)     NULL,
    EstimatedRollbackSeconds DECIMAL(10,1)   NULL,
    -- Execution plan tracking
    QueryHash               VARBINARY(8)     NULL,
    QueryPlanHash           VARBINARY(8)     NULL,
    PlanRecommendation      NVARCHAR(MAX)    NULL,
    QSPlanForced            BIT              NULL DEFAULT 0,
    -- LLM audit trail (written by tools/audit_log.py's INSERT_KILL_SQL)
    LLMReasoning            NVARCHAR(MAX)    NULL,
    RCAReport               NVARCHAR(MAX)    NULL,
    RuleTriggered           INT              NULL,
    -- Metadata
    WaitDurationMs          BIGINT           NULL,
    VictimCount             INT              NULL,
    KillStatus              NVARCHAR(50)     NULL,
    DryRun                  BIT              NOT NULL DEFAULT 0,
    CorrelationID           NVARCHAR(100)    NULL
);
GO

-- Immutability: AFTER UPDATE/DELETE trigger rejects all modifications
CREATE TRIGGER trg_KillAudit_Immutable
ON dbo.KillAuditLog AFTER UPDATE, DELETE AS
BEGIN
    RAISERROR('KillAuditLog is immutable.', 16, 1);
    ROLLBACK;
END;
GO

CREATE TABLE dbo.BlockingEventLog (
    EventID          UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID() PRIMARY KEY,
    EventTimeUTC     DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME(),
    ServerName       NVARCHAR(100)    NOT NULL,
    CorrelationID    NVARCHAR(100)    NULL,
    -- Head blocker
    HeadBlockerSPID  INT              NULL,
    HeadBlockerLogin NVARCHAR(128)    NULL,
    BlockerDatabase      NVARCHAR(128)    NULL,   -- DB the head blocker session is in
    BlockerSQLText       NVARCHAR(MAX)    NULL,   -- statement the blocker was/is running
    BlockerParentObject  NVARCHAR(512)    NULL,   -- schema.name [TYPE] if inside a proc/function/trigger
    -- Victims (blocked sessions)
    VictimSPIDs      NVARCHAR(1000)   NULL,   -- comma-separated list e.g. "207, 208"
    VictimLogins     NVARCHAR(2000)   NULL,   -- comma-separated victim logins
    VictimDatabases  NVARCHAR(500)    NULL,   -- deduplicated comma-separated victim DBs
    VictimSQLText        NVARCHAR(MAX)    NULL,   -- blocked statement(s), sep by "---"
    VictimParentObjects  NVARCHAR(1000)   NULL,   -- comma-sep schema.name [TYPE] per victim
    -- Timing
    WaitDurationMs   BIGINT           NULL,   -- longest victim wait at detection time
    VictimCount      INT              NULL,
    -- Lock details
    WaitType         NVARCHAR(60)     NULL,   -- e.g. LCK_M_U, LCK_M_X, LCK_M_S
    LockResource     NVARCHAR(500)    NULL,   -- raw resource_description from dm_os_waiting_tasks
    LockObjectName   NVARCHAR(256)    NULL,   -- resolved table/object e.g. "dbo.Orders"
    LockIndexName    NVARCHAR(256)    NULL,   -- resolved index e.g. "PK_Orders"; NULL for heap/OBJECT locks
    -- Decision
    DecisionTaken    NVARCHAR(20)     NULL,
    DecisionReason   NVARCHAR(2000)   NULL,
    RiskLevel        NVARCHAR(20)     NULL,
    DryRun           BIT              NOT NULL DEFAULT 0,
    HasBlockerPlan   BIT              NOT NULL DEFAULT 0   -- 1 if plan XML stored in RCASnapshotLog
);
GO

CREATE TABLE dbo.RCASnapshotLog (
    SnapshotID          UNIQUEIDENTIFIER NOT NULL DEFAULT NEWID() PRIMARY KEY,
    KillCorrelationID   NVARCHAR(100)    NULL,
    SnapshotTimeUTC     DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME(),
    ServerName          NVARCHAR(100)    NOT NULL,
    KilledSPID          INT              NULL,
    DMVSnapshotJSON     NVARCHAR(MAX)    NULL,
    RCAReportMarkdown   NVARCHAR(MAX)    NULL,
    BlockerPlanXML      NVARCHAR(MAX)    NULL   -- XML showplan for the head blocker
);
GO

-- Indexes for common query patterns
CREATE INDEX IX_KillAuditLog_ServerTime ON dbo.KillAuditLog (ServerName, KillTimeUTC DESC);
CREATE INDEX IX_BlockingEventLog_ServerTime ON dbo.BlockingEventLog (ServerName, EventTimeUTC DESC);
GO
