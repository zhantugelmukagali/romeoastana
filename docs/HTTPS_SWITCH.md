# HTTPS для проверки установки

Серверная основа ещё не является готовой бонусной программой.
Переключение заменяет старый пилот по https://loyalty.addwise.kz новой серверной версией.
Данные пилота не мигрируют и не удаляются. По /admin новая версия требует вход из МойСклад.

После запуска app/db и readiness выполнить в отдельном checkout ~/bonuspro-release:

```bash
git pull --ff-only
sudo python3 deploy/switch_https.py
```

Скрипт проверяет старый web, известные TLS volumes, локальную готовность, Compose,
скачивает Caddy и проверяет конфигурацию. Сохраняет копию старого Caddyfile в backups.
Затем останавливает только старый web и запускает web проекта bonuspro-release.
Новый web использует собственную сеть release и прежние TLS volumes. Связь сохраняется
после пересоздания контейнеров; ручного docker network connect нет.
HTTP readiness проверяется через HTTPS с проверкой доверия сертификату и имени домена.
Если запуск или проверка HTTPS не удались, пытается остановить новый web и запускает старый.

Все дальнейшие команды Compose должны включать оба файла:
```bash
sudo docker compose --env-file .env.release -p bonuspro-release -f compose.release.yaml -f compose.https.yaml ps
```

Ручной откат, если переключение успешно, но позднее обнаружена проблема:
```bash
sudo docker compose --env-file .env.release -p bonuspro-release -f compose.release.yaml -f compose.https.yaml stop web &&
sudo docker start romeo-customer-web-1
```

Не запускать старый web одновременно с новым: они занимают 80/443.
Не удалять romeo-customer_caddydata/caddyconfig — их использует новая версия.
Повторный запуск switch_https.py при уже существующем release web намеренно блокируется.
Скрипт не обновляет образ app, миграции, ключи или состояние регистрации МойСклад.
Успешный HTTPS не подтверждает настоящую установку, начисление или списание.
