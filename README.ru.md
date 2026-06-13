# nginx / OpenResty live monitor

Терминальный монитор производительности nginx: latency (перцентили), статусы,
топ медленных URL, соединения из `stub_status`, метрики хоста и алерты.
Обновляется раз в N секунд по скользящему окну.

## Установка (на сервере с nginx/OpenResty)

```bash
pip install -r requirements.txt
```

## Запуск

```bash
python3 nginx_monitor.py \
  --access-log /usr/local/openresty/nginx/logs/access.log \
  --status-url http://127.0.0.1/nginx_status \
  --interval 2 --window 60 --bell
```

Выход — `Ctrl+C`. Полезные флаги: `--no-system`, `--from-start`,
пороги алертов `--th-5xx 1 --th-p99 1000 --th-disk 90 --th-cpu 90 --th-mem 90`.

## Требования к логам

Для перцентилей нужен `request_time` в access-логе. Поддерживаются два формата:

**perf (текстовый):**
```nginx
log_format perf '$remote_addr - $remote_user [$time_local] "$request" '
                '$status $body_bytes_sent "$http_referer" "$http_user_agent" '
                'rt=$request_time uct=$upstream_connect_time '
                'uht=$upstream_header_time urt=$upstream_response_time';
access_log /usr/local/openresty/nginx/logs/access.log perf;
```

**JSON (предпочтительно, надёжнее парсится):**

`status`, `bytes`, `request_time` — числа (без кавычек); `upstream_*` времена —
в кавычках, т.к. при отсутствии апстрима nginx пишет туда `-`. `escape=json` обязателен.

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

Применить: `nginx -t && nginx -s reload`.
```
