SkyAgentManager - Windows x64 development build

Extract the entire ZIP to a local writable folder.
Run SkyAgentManager\SkyAgentManager.exe.
Keep the _internal folder beside the executable. Do not copy only the EXE.

Requires a Windows x64 environment supported by the bundled Python/Qt runtime.
This development package is unsigned; Windows may show a publisher warning.
Do not disable security software. Verify the archive's SHA-256 before use.

Close older versions and back up application data before upgrading.
Do not run multiple versions against the same database.
Schema v6 databases must not be opened by version 0.2.24 or older.

Database: %LOCALAPPDATA%\SkyAgentManager\manager.sqlite3 (encrypted).
Encryption keys use the system credential store; no application startup password.
Cross-device migration requires the application's portable encrypted backup.
Do not copy just the database to another computer.

Frozen self-checks use temporary synthetic data, not real accounts or credentials.
Production compatibility and manual Windows credential-store tests remain pending.
Silver-card redemption and complete real task execution are not implemented.
