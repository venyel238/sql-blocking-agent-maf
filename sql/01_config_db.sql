-- ============================================================
-- 01_config_db.sql — AgentConfigDB DDL
-- Run on the management SQL Server instance
-- ============================================================

CREATE DATABASE AgentConfigDB;
GO
USE AgentConfigDB;
GO

CREATE TABLE dbo.GlobalConfig (
    ConfigKey   NVARCHAR(100) PRIMARY KEY,
    ConfigValue NVARCHAR(MAX) NOT NULL,
    UpdatedAt   DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
);

-- Default values
-- Live-tunable: UPDATE dbo.GlobalConfig SET ConfigValue='<new>' WHERE ConfigKey='<key>'
-- Note: config is loaded once at agent startup. Changes take effect on restart.
INSERT INTO dbo.GlobalConfig (ConfigKey, ConfigValue) VALUES
('MaxKillsPerHour',        '10'),
('DryRunGlobal',           'true'),
('PollIntervalSeconds',    '15'),
('KillThresholdMs',        '30000'),
('LogSizeKillThresholdGB', '10'),
('DbaAlertEmail',          'evhdba@evolent.com'),
('PlanLookbackHours',      '24');
GO

CREATE TABLE dbo.ExclusionWindows (
    ID           INT IDENTITY PRIMARY KEY,
    WindowName   NVARCHAR(100) NOT NULL,
    StartTimeUTC TIME          NOT NULL,
    EndTimeUTC   TIME          NOT NULL,
    DaysOfWeek   NVARCHAR(100) NOT NULL DEFAULT '*',  -- '*' = all days, or 'Mon,Tue,Wed'
    ServerID     NVARCHAR(100) NULL,
    IsEnabled    BIT           NOT NULL DEFAULT 1
);
GO

CREATE TABLE dbo.TargetServers (
    ServerID             INT IDENTITY PRIMARY KEY,
    ServerName           NVARCHAR(100) NOT NULL UNIQUE,
    ServerType           NVARCHAR(20)  NOT NULL DEFAULT 'OLTP'
        CONSTRAINT chk_server_type CHECK (ServerType IN ('OLTP')),
    KillConnectionString NVARCHAR(MAX) NULL,
    IsActive             BIT           NOT NULL DEFAULT 1,
    Enabled              BIT           NOT NULL DEFAULT 1
);
GO
