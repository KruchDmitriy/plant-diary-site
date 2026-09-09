# Дневник растений

Снапшот: 209 растений · 850 фото · 43 события.

Статический сайт публикуется на GitHub Pages. Airtable остаётся источником
истины: GitHub Actions формирует JSON во время сборки, а браузер не получает
токен Airtable и не обращается к Airtable напрямую.

## Локальный просмотр

Из папки проекта:

```bash
python3 -m http.server 8080
```

Откройте `http://localhost:8080`.

Проверить текущий snapshot без сети и без Airtable-токена:

```bash
python3 -B tools/sync_airtable.py --validate-only
python3 -m venv .venv
.venv/bin/pip install -r plant_api/requirements.txt -r plant_api/requirements-dev.txt
.venv/bin/python -B -m unittest discover -s tests -v
```

## GitHub Pages

Workflow `.github/workflows/deploy-pages.yml`:

- при push в `main` проверяет данные и публикует сайт;
- при наличии `AIRTABLE_TOKEN` сначала обновляет JSON из Airtable;
- ежедневно в `03:17 UTC` (`06:17` по Москве) обязательно запускает sync;
- допускает ручной запуск из вкладки Actions;
- публикует только `index.html`, `.nojekyll`, `assets/` и три публичных JSON.

Для первого запуска:

1. Создайте пустой GitHub-репозиторий и отправьте в него ветку `main`.
2. В `Settings → Pages → Build and deployment` выберите `GitHub Actions`.
3. В `Settings → Secrets and variables → Actions` добавьте repository secret
   `AIRTABLE_TOKEN`. Токену достаточно read-доступа к данным базы
   `Дневник растений` (`data.records:read`, только эта база).
4. Запустите workflow вручную или отправьте новый commit в `main`.

Секрет Yandex Object Storage для деплоя не нужен: сайт использует только уже
публичные объекты `web/*`. `originals/*` не публикуются.

## Airtable → JSON

Ручной sync выполняется только через переменную окружения:

```bash
export AIRTABLE_TOKEN='…'
python3 -B tools/sync_airtable.py --dry-run
python3 -B tools/sync_airtable.py
unset AIRTABLE_TOKEN
```

`--dry-run` получает и проверяет все страницы Airtable, но не меняет файлы.
Обычный запуск заменяет JSON только после успешной полной проверки.

Скрипт создаёт:

- `data/plants.json`;
- `data/photos.json`;
- `data/events.json`.

Защитный baseline находится в `data/sync_baseline.json`. Он не позволяет
автоматически удалить известные Plant ID `1–209`, переименовать без проверки
карточки `#1–34`, уменьшить контрольные объёмы (209/850/43), опубликовать
неуникальные `Web key`, незагруженные фото или битые связи. Для осознанного
изменения baseline нужно отдельно проверить данные и обновить этот файл.

Публичный URL фото всегда строится из `Web key`:

`https://storage.yandexcloud.net/plant-diary-photos/<Web key>`

Токен Airtable читается только из `AIRTABLE_TOKEN`; в frontend и JSON он не
попадает. `.env` и варианты `.env.*` исключены через `.gitignore`.

## Добавление растений и фото из приватного GPT

В `plant_api/` находится отдельный защищённый API для Custom GPT. Он работает с
текущими таблицами Airtable — новые служебные поля не нужны — и умеет:

- добавлять фотографии и комментарий к существующему Plant ID;
- создавать новый физический экземпляр с очередным Plant ID;
- уточнять название, латинское имя и сорт без изменения Plant ID;
- выбирать уже загруженное фото как превью карточки;
- добавлять событие в дневник.

По умолчанию запись выключена. Даже после включения каждое изменение требует
явного подтверждения пользователя; создание растения дополнительно фиксирует
заранее показанный следующий ID и безопасно отклоняет повторный запрос.

Локальная проверка и требования к HTTPS-хостингу описаны в
[`plant_api/README.md`](plant_api/README.md). GitHub Pages остаётся статическим
публичным сайтом; приватный API разворачивается отдельно и после записи запускает
существующий Airtable → JSON → Pages workflow.

## Сделать WebP публичными, не открывая originals

Скопируйте `tools/publish_web_prefix.py` рядом с вашим uploader `.env` и запустите:

```bash
pip install boto3 python-dotenv
python3 tools/publish_web_prefix.py
```

Скрипт **сохраняет существующие statements bucket policy** и добавляет только anonymous `s3:GetObject` для:

`arn:aws:s3:::plant-diary-photos/web/*`

Префикс `originals/*` этим правилом не публикуется. Публичный list бакета не включается.

Проверка после применения политики (иногда настройки распространяются не мгновенно):

```bash
python3 tools/check_public_web.py
```

## Данные

- `data/plants.json` — 209 карточек (ID + имя + UI-группа/статус + cover/counts).
- `data/photos.json` — 850 WebP URL и связи с Plant ID.
- `data/events.json` — 43 текущих события.

### Важно

Существующие Plant ID не перенумеровываются и не используются повторно.
Групповые фотографии сохраняют все связи `plant_ids`; пять исторических фото
утраченной фиалки могут оставаться без текущей связи.
