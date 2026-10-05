param(
    [string]$StateDirectory,
    [string]$ContainerName = 'xhs-werss-local',
    [ValidateRange(1024,65535)][int]$Port = 8002
)
$ErrorActionPreference = 'Stop'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker is required.' }
if ($ContainerName -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$') { throw 'Invalid container name.' }
$werssProjectDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
if (-not $StateDirectory) { $StateDirectory = Join-Path $werssProjectDirectory 'Codex/state/we-mp-rss-local' }
$werssStateDirectory = [IO.Path]::GetFullPath($StateDirectory)
if (Test-Path -LiteralPath $werssStateDirectory) { throw 'The state directory already exists; inspect it before deploying.' }
$werssExisting = docker ps -a --filter "name=^/$ContainerName`$" --format '{{.Names}}'
if ($LASTEXITCODE -ne 0) { throw 'Docker is unavailable.' }
if ($werssExisting) { throw 'The container name is already in use.' }
$werssProbe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$Port)
try { $werssProbe.Start() } finally { $werssProbe.Stop() }
$null = New-Item -ItemType Directory -Path $werssStateDirectory
$null = New-Item -ItemType Directory -Path (Join-Path $werssStateDirectory 'data')
$werssRandom = [byte[]]::new(32)
$werssRandomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $werssRandomGenerator.GetBytes($werssRandom) } finally { $werssRandomGenerator.Dispose() }
$werssPassword = [Convert]::ToBase64String($werssRandom)
$werssCredentials = @{ username = 'local-admin'; password = $werssPassword } | ConvertTo-Json
$werssCredentialsPath = Join-Path $werssStateDirectory 'admin-credentials.json'
[IO.File]::WriteAllText($werssCredentialsPath,$werssCredentials,[Text.UTF8Encoding]::new($false))
$werssImage = 'ghcr.io/rachelos/we-mp-rss@sha256:af771f21b3f7958a5dea16911fba050a6d7b92eac2fb2499c467c1b11f07ef34'
docker run -d --name $ContainerName --restart unless-stopped `
    --publish "127.0.0.1:${Port}:8001" `
    --mount "type=bind,source=$werssStateDirectory/data,target=/app/data" `
    --mount "type=bind,source=$PSScriptRoot/bootstrap.py,target=/app/codex-bootstrap.py,readonly" `
    --mount "type=bind,source=$PSScriptRoot/weread_integration.py,target=/app/codex_weread.py,readonly" `
    --mount "type=bind,source=$PSScriptRoot/launch.sh,target=/app/codex-launch.sh,readonly" `
    --mount "type=bind,source=$werssCredentialsPath,target=/app/codex-admin.json,readonly" `
    --env 'ENABLE_JOB=False' --env 'GATHER.CONTENT_AUTO_CHECK=False' `
    --env 'ARTICLE_STATS_REFRESH_ENABLED=False' --env 'WE_RSS.AUTH=False' `
    --env 'GATHER.MODEL=weread_mp' --env 'GATHER.CONTENT=True' `
    --env 'WEREAD_CONTENT_INTERVAL=2' --env 'REDIS_SERVER_ENABLED=False' --env 'REDIS_URL=' `
    --env 'DB=sqlite:////app/data/db.db' --env "RSS_BASE_URL=http://127.0.0.1:$Port/" `
    --env 'RSS_LOCAL=False' --env 'DEBUG=False' `
    --log-opt 'max-size=5m' --log-opt 'max-file=2' `
    --entrypoint /bin/bash $werssImage /app/codex-launch.sh
if ($LASTEXITCODE -ne 0) { throw 'Container creation failed; the new state directory is preserved for inspection.' }
Write-Output "Local WeRSS started on http://127.0.0.1:$Port/. Private credentials: $werssCredentialsPath"
