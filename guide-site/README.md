# AtcivPerson Guide Site

Отдельный статический сайт-руководство. Не зависит от Python-приложения, Telegram-бота, API или базы данных.

## Локальный просмотр

Откройте `index.html` в браузере либо запустите:

```bash
python3 -m http.server 8088 --directory guide-site
```

Затем откройте `http://127.0.0.1:8088`.

## Размещение через Caddy

Скопируйте каталог `guide-site` на сервер и создайте отдельный Caddy site block:

```caddy
guide.example.com {
    root * /srv/atcivperson-guide
    encode zstd gzip
    file_server
}
```

Каталог можно смонтировать в контейнер Caddy read-only. Для сайта не нужны Node.js, сборка frontend, API или доступ к базе данных.

## Состав

- `index.html` — адаптивное руководство на русском языке, поиск по функциям, навигация, раскрывающиеся инструкции.
