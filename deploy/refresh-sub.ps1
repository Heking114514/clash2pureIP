# 定时刷新订阅：本机拉取（不被订阅站拦截）→ 推送服务器缓存 → 服务器转换
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File refresh-sub.ps1
$ErrorActionPreference = 'Stop'
$SubUrl = 'https://example.com/api/v1/client/subscribe?token=REPLACE_ME'
$Server = 'http://<server>:8090'
$WebUser = 'admin'
$WebPass = 'change-me'
$PortStart = 10001
$Log = Join-Path $PSScriptRoot 'refresh-sub.log'

function Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $Log -Value $line
    Write-Host $line
}

Log '=== refresh start ==='

# 1. fetch subscription locally (Clash UA passes Cloudflare)
$tmp = Join-Path $env:TEMP 'sub-fetch.yaml'
$code = & curl.exe -sS -L --max-time 40 -A 'ClashMeta/1.19.0' -o $tmp -w '%{http_code}' $SubUrl
if ($LASTEXITCODE -ne 0 -or $code -ne '200') {
    Log "fetch failed: HTTP $code (exit $LASTEXITCODE)"
    exit 1
}
$bytes = [IO.File]::ReadAllBytes($tmp)
Log ("fetched {0} bytes" -f $bytes.Length)

$authPair = "${WebUser}:${WebPass}"
$authHeader = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($authPair))

# 2. push to server cache (raw bytes, no mangling)
$up = Invoke-RestMethod -Method Post -Uri "$Server/api/upload-sub" `
    -Headers @{ Authorization = $authHeader; 'Content-Type' = 'text/yaml' } `
    -Body $bytes -TimeoutSec 40
Log ("upload: ok={0} chars={1} cached_at={2}" -f $up.ok, $up.bytes, $up.cached_at)

# 3. server-side convert (falls back to cache when direct fetch is blocked)
$conv = Invoke-RestMethod -Method Post -Uri "$Server/api/convert" `
    -Headers @{ Authorization = $authHeader; 'Content-Type' = 'application/json' } `
    -Body (@{ url = $SubUrl; port_start = $PortStart } | ConvertTo-Json -Compress) `
    -TimeoutSec 150
if (-not $conv.ok) {
    Log "convert FAILED: $($conv.error)"
    exit 1
}
Log ("convert: nodes={0} ready={1} source_used={2} note={3}" -f $conv.nodes, $conv.ready, $conv.source_used, $conv.fetch_note)

Log '=== refresh done ==='
