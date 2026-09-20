# Owner action: host-nginx для stg.stickme.art — таймаут 120 с → 300 с

**Зачем:** генерация превью/FULL может идти дольше 2 минут. Сейчас host-nginx на VPS рвёт соединение через
120 с и браузер получает «504 Gateway Time-out», хотя backend продолжает работать (compose-nginx и gunicorn
уже стоят на 300 с). После правки браузер будет ждать до 5 минут.

**Где:** VPS 176.119.159.141, файл `/etc/nginx/sites-available/stg.stickme.art`, строки 16–17
(внутри `location / { … }`).

## Вариант 1 — одной командой (под root)

```bash
ssh root@176.119.159.141
sed -i 's/proxy_read_timeout 120s;/proxy_read_timeout 300s;/; s/proxy_send_timeout 120s;/proxy_send_timeout 300s;/' /etc/nginx/sites-available/stg.stickme.art \
  && nginx -t && systemctl reload nginx
```

## Вариант 2 — через редактор

```bash
ssh root@176.119.159.141
nano /etc/nginx/sites-available/stg.stickme.art
```

Найти:

```
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
```

Заменить на:

```
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
```

Сохранить (Ctrl+O, Enter, Ctrl+X), затем:

```bash
nginx -t && systemctl reload nginx
```

## Проверка

```bash
grep -n "timeout" /etc/nginx/sites-available/stg.stickme.art
```

Ожидаемо: обе строки со значением `300s`. `nginx -t` должен печатать `syntax is ok` / `test is successful`.

`reload` не прерывает соединения и не затрагивает соседние сайты (dev-*, ayla-*). Ничего в docker/compose
менять не нужно. После выполнения — сообщите оркестратору, Agent A проверит.
