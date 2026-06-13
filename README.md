# nginx / OpenResty live monitor

Terminal monitor for nginx/OpenResty performance: latency (percentiles), status
codes, slowest URLs, connection stats from `stub_status`, host metrics and alerts.
Refreshes every N seconds over a sliding window.

> Russian version: [README.ru.md](README.ru.md)

## Install (on the server running nginx/OpenResty)

```bash
pip install -r requirements.txt
```

## Run

```bash
python3 nginx_monitor.py \
  --access-log /usr/local/openresty/nginx/logs/access.log \
  --status-url http://127.0.0.1/nginx_status \
  --interval 2 --window 60 --bell
```

Quit with `Ctrl+C`. Useful flags: `--no-system`, `--from-start`,
alert thresholds `--th-5xx 1 --th-p99 1000 --th-disk 90 --th-cpu 90 --th-mem 90`.

## Log requirements

Percentiles need `request_time` in the access log. Two formats are supported.

**perf (plain text):**
```nginx
log_format perf '$remote_addr - $remote_user [$time_local] "$request" '
                '$status $body_bytes_sent "$http_referer" "$http_user_agent" '
                'rt=$request_time uct=$upstream_connect_time '
                'uht=$upstream_header_time urt=$upstream_response_time';
access_log /usr/local/openresty/nginx/logs/access.log perf;
```

**JSON (preferred — parsed by key, no regex):**

`status`, `bytes`, `request_time` are numbers (unquoted); `upstream_*` times are
quoted because nginx writes `-` when there is no upstream. `escape=json` is required.

```nginx
log_format json_perf escape=json '{'
  '"time":"$time_iso8601",'
  '"remote_addr":"$remote_addr",'
  '"remote_user":"$remote_user",'
  '"request":"$request",'
  '"uri":"$uri",'
  '"args":"$args",'
  '"status":$status,'
  '"bytes":$body_bytes_sent,'
  '"referer":"$http_referer",'
  '"ua":"$http_user_agent",'
  '"request_time":$request_time,'
  '"upstream_connect_time":"$upstream_connect_time",'
  '"upstream_header_time":"$upstream_header_time",'
  '"upstream_time":"$upstream_response_time"'
'}';
access_log /usr/local/openresty/nginx/logs/access.log json_perf;
```

`stub_status`:
```nginx
location /nginx_status { stub_status; allow 127.0.0.1; deny all; }
```

Apply changes: `nginx -t && nginx -s reload`.

## Recognized JSON keys

The parser reads these keys (extra keys are kept in the log but ignored):

| Key | Meaning |
|---|---|
| `status` | HTTP status code (number) |
| `request_time` | total request time, seconds (drives p50–p99) |
| `upstream_time` / `upstream_response_time` | backend time, seconds |
| `uri` / `request` | URL for "top slow URLs" grouping |
| `remote_addr` | client IP |
| `bytes` / `body_bytes_sent` | response size |

A `-` value (no upstream) is treated as "no data" and excluded from percentiles.
