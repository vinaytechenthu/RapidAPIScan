# RapidPulse on a Windows EC2 instance

## Option 1: one file, run on the instance

1. **Launch the instance:** Windows Server 2022, t3.large or larger, a 50 GB data volume as `D:`, in the same VPC as your applications.
2. **Security group:** allow inbound **TCP 4400** from your application servers and your own network only.
3. **Copy the RapidPulse folder** to the instance, for example to the Desktop.
4. **Double-click `install-rapidpulse.bat`** and accept the Administrator prompt.

The installer does everything else:

| Step | What it does |
|---|---|
| Python | Uses Python 3.10+ if installed; otherwise downloads and installs Python 3.11 silently |
| Files | Copies RapidPulse to `C:\RapidPulse` |
| Dependencies | Creates a private Python environment and installs the packages |
| Settings | Writes `C:\RapidPulse\config\pulse.local.yaml`: data path, port, a generated **ingest key** and **admin password**, and your alert webhooks. Only Administrators and SYSTEM can read the file |
| Firewall | Opens TCP 4400 in Windows Firewall |
| Auto-start | Registers the `RapidPulseServer` task: starts at boot, runs as SYSTEM, restarts if it stops |
| Check | Waits until the server answers, then prints the dashboard URL, login and ingest key |

Data and logs go to `D:\RapidPulse`, or to `C:\RapidPulseData` if there is no `D:` drive.

### Options

Pass these to the `.bat` or to `deploy\install-server.ps1`:

| Option | Example | Meaning |
|---|---|---|
| `-AllowFrom` | `-AllowFrom 10.0.0.0/16` | Restrict the firewall rule to these IPs or CIDRs |
| `-Webhook` | `-Webhook https://hooks.slack.com/...` | Where alerts are sent (Slack, Teams or a generic webhook) |
| `-AdminPassword` / `-IngestKey` | `-AdminPassword S3cure!` | Use your own values instead of generated ones |
| `-Port` | `-Port 8443` | Use a port other than 4400 |
| `-DataDir` / `-InstallDir` | `-DataDir E:\RapidPulse` | Choose different folders |
| `-NoUiLogin` | | No dashboard login (not recommended) |
| `-NoService` | | Trial run: no firewall rule and no auto-start |
| `-Uninstall` | | Removes the task and firewall rule; keeps files and data |

**Upgrading:** copy the new version and run the `.bat` again. Your key, password, settings and data are kept.

## Option 2: fully automatic at launch (EC2 User Data)

A script running inside an instance can't create that instance. Step 1 above therefore has to be done from AWS: the console, CLI, CloudFormation or Terraform. You can make the launch itself install RapidPulse, with no RDP needed:

1. Zip the RapidPulse folder's **contents** and upload it to S3, for example `s3://my-bucket/rapidpulse.zip`.
2. Store the admin password in SSM Parameter Store as a **SecureString**, for example `/rapidpulse/admin-password`.
3. Give the instance an IAM role that can read that S3 object and that parameter.
4. Paste this into the instance's **User Data** when you launch it:

```powershell
<powershell>
$ErrorActionPreference = "Stop"
Read-S3Object -BucketName "my-bucket" -Key "rapidpulse.zip" -File "C:\rapidpulse.zip"
Expand-Archive "C:\rapidpulse.zip" "C:\RapidPulseSetup" -Force
$pw = (Get-SSMParameter -Name "/rapidpulse/admin-password" -WithDecryption $true).Value
& "C:\RapidPulseSetup\deploy\install-server.ps1" -AdminPassword $pw -AllowFrom 10.0.0.0/16 `
    -Webhook "https://hooks.slack.com/services/..." *> "C:\RapidPulseSetup\install.log"
</powershell>
```

The AWS Tools for PowerShell (`Read-S3Object`, `Get-SSMParameter`) are preinstalled on Windows AMIs. User Data runs as Administrator.

When the instance has booted, RapidPulse is running at `http://<private-ip>:4400`. The ingest key is in `C:\RapidPulse\config\pulse.local.yaml`, and the install output is in `C:\RapidPulseSetup\install.log`.

## Then

- **Your APIs:** set `OTEL_EXPORTER_OTLP_ENDPOINT=http://<rapidpulse-ip>:4400` and `OTEL_EXPORTER_OTLP_HEADERS=x-pulse-key=<key>`, and turn on OpenTelemetry auto-instrumentation. See the README, "Monitoring your own application".
- **Your servers:** run `deploy\install-agent.ps1 -Server http://<rapidpulse-ip>:4400 -Key <key> -App <application>` on each one.
