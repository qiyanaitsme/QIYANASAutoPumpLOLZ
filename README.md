# 🚀 QIYANA AUTO-BUMP BOT для Lolz.live

Production-ready Telegram бот для автоматического поднятия тем на форуме Lolz.live с современной архитектурой, batch API и полной типизацией.

## ✨ Возможности

- ➕ **Добавление тем** - через запятую (12345, 67890, 11111) с batch API
- 🗑️ **Удаление тем** - через интерактивное меню
- 📋 **Список тем** - с датой последнего поднятия
- 🚀 **Ручное поднятие** - поднять все темы немедленно через batch API
- ⏰ **Автоподнятие** - каждые N часов автоматически
- 📊 **Статистика** - успешность, количество поднятий, uptime
- 🔔 **Уведомления** - о каждом поднятии темы
- 🛡️ **Обработка ошибок** - rate limits, network failures, invalid tokens
- ⚡ **Batch API** - до 10 тем за один запрос (10x быстрее)

## 🎮 Интерфейс

```
┌─────────────────────────────────┐
│  ➕ Добавить темы  │ 📋 Список тем  │
│  🗑️ Удалить тему  │ 🚀 Поднять темы │
│  📊 Статистика    │ 👤 Автор       │
└─────────────────────────────────┘
```

## 📦 Установка

### Требования

- Python 3.12+
- pip или uv

### 1. Установите зависимости

```bash
pip install -r requirements.txt
```

### 2. Получите токены

**Telegram Bot Token:**
1. Напишите [@BotFather](https://t.me/BotFather)
2. Создайте бота: `/newbot`
3. Скопируйте токен

**Lolz API Token:**
1. Перейдите на [zelenka.guru/account/api](https://zelenka.guru/account/api)
2. Создайте токен с правами `read`, `post`
3. Скопируйте токен

### 3. Настройте config.json

```json
{
    "bot": {
        "api_token": "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz",
        "img_url": "https://wallpapers-clan.com/wp-content/uploads/2024/04/dark-anime-girl-with-red-eyes-desktop-wallpaper-preview.jpg",
        "author_url": "https://lolz.live/kqlol/"
    },
    "api": {
        "base_url": "https://prod-api.lolz.live",
        "auth_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
        "batch_size": 10
    },
    "database": {
        "path": "threads.db"
    },
    "scheduling": {
        "bump_interval_hours": 12,
        "bump_delay_seconds": 2,
        "enable_auto_bump": true
    }
}
```

### 4. Запустите бота

```bash
python app.py
```

## 📖 Использование

### Добавление тем

1. Нажмите **➕ Добавить темы**
2. Введите ID через запятую: `12345, 67890, 11111`
3. Бот автоматически получит названия тем через batch API (до 10 за запрос)

**Пример:**
```
Ввод: 9247920, 9247921, 9247922

Результат:
✅ 9247920 - Qiyanas steam idler
✅ 9247921 - Discord bot
✅ 9247922 - VPN service
```

**Batch API преимущества:**
- Добавление 10 тем = 1 batch запрос вместо 10 обычных
- Добавление 25 тем = 3 batch запроса вместо 25 обычных
- Экономия API лимитов в 10 раз

### Удаление темы

1. Нажмите **🗑️ Удалить тему**
2. Выберите тему из списка
3. Подтвердите удаление

### Ручное поднятие

1. Нажмите **🚀 Поднять темы**
2. Бот поднимет все темы через batch API (до 10 за запрос)
3. Получите уведомление о каждой теме:
   ```
   [1/3] ✅ Тема 9247920 поднята успешно
   [2/3] ✅ Тема 9247921 поднята успешно
   [3/3] ✅ Тема 9247922 поднята успешно
   ```

**Batch API преимущества:**
- Поднятие 10 тем = 1 batch запрос (0.1 * 10 = 1 batch)
- Поднятие 50 тем = 5 batch запросов вместо 50 обычных
- Скорость выполнения увеличена в ~10 раз

### Просмотр списка

Нажмите **📋 Список тем** - покажет:
- ID темы
- Название темы
- Дату последнего поднятия

### Статистика

Нажмите **📊 Статистика** - покажет:
- Количество тем
- Готовые к поднятию
- Всего попыток
- Успешность (%)
- Настройки интервала
- Время работы бота

## ⚙️ Настройки

### Интервал автоподнятия

В `config.json` измените `bump_interval_hours`:

| Значение | Интервал |
|----------|----------|
| `12` | 12 часов |
| `6` | 6 часов |
| `24` | 24 часа |

### Размер batch запросов

В `config.json` измените `batch_size`:

| Значение | Описание |
|----------|----------|
| `10` | Максимум (рекомендуется) - 10 тем за запрос |
| `5` | Средний - 5 тем за запрос |
| `1` | Отключить batch - по 1 теме |

**Примечание:** Batch API Lolz.live поддерживает до 10 запросов в одном batch. Каждый запрос = 0.1 batch, итого 10 запросов = 1 полный batch.

### Тестовый режим (5 минут)

```json
"scheduling": {
    "bump_interval_hours": 0.0833,
    "bump_delay_seconds": 1,
    "enable_auto_bump": true
}
```

## 🔄 Как работает автоподнятие

1. Запускаете бота
2. Вручную поднимаете темы через 🚀 (первый раз)
3. Бот ждет указанный интервал (например, 12 часов)
4. Автоматически поднимает темы, у которых прошло 12+ часов
5. Повторяет каждые 12 часов

**Пример:**
```
00:00 - Запуск бота
00:05 - Вы вручную нажали "Поднять темы"
12:05 - Автоматическое поднятие
24:05 - Автоматическое поднятие
36:05 - Автоматическое поднятие
```

## 🏗️ Архитектура

### Модульная структура

```
app.py              # Main bot logic with FSM
config_manager.py   # Type-safe configuration
api_client.py       # Lolz batch API client with retry logic
database.py         # SQLite with connection pooling
```

### Ключевые улучшения

- **Batch API**: До 10 тем за один запрос (10x эффективнее)
- **Type Safety**: Полная типизация с Python 3.12+ (PEP 695)
- **SOLID Principles**: Каждый модуль имеет одну ответственность
- **Connection Pooling**: Эффективное управление соединениями
- **Error Handling**: Специфичные исключения для каждого случая
- **Async Context Managers**: Автоматическая очистка ресурсов
- **Immutable Config**: Frozen dataclasses для безопасности
- **Logging**: Структурированные логи с rotation
- **Graceful Shutdown**: Корректное завершение всех задач

### Batch API Implementation

**Как работает:**
```python
# Старый способ (10 запросов):
for thread_id in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
    POST /threads/{thread_id}/bump

# Новый способ (1 batch запрос):
POST /batch
[
  {"uri": "/threads/1/bump", "method": "POST"},
  {"uri": "/threads/2/bump", "method": "POST"},
  ...
  {"uri": "/threads/10/bump", "method": "POST"}
]
```

**Преимущества:**
- Экономия API лимитов в 10 раз
- Скорость выполнения увеличена в ~10 раз
- Меньше нагрузка на сеть
- Атомарная обработка результатов

### Edge Cases

- Rate limiting от API (обработка в batch ответах)
- Network timeouts (retry для batch запросов)
- Invalid tokens (валидация перед batch)
- Partial batch failures (индивидуальная обработка каждого результата)
- Concurrent access (connection pooling)
- Database locks (async context managers)
- Memory leaks prevention (frozen dataclasses, proper cleanup)

## 📝 Логи

- **Консоль** - основные события
- **bot.log** - детальные логи с traceback

## 🔧 Технические детали

- **Python**: 3.12+
- **aiogram**: 3.4.1
- **aiohttp**: 3.10.0+
- **aiosqlite**: 0.20.0+
- **База данных**: SQLite с индексами
- **API**: https://prod-api.lolz.live
- **Batch API**: До 10 запросов за 1 batch (каждый = 0.1 batch)

## 🐛 Troubleshooting

### Ошибка: "bot.api_token not configured"

Заполните `config.json` реальными токенами.

### Ошибка: "Invalid API token"

Проверьте токен на [zelenka.guru/account/api](https://zelenka.guru/account/api).

### Ошибка: "Network error"

Проверьте интернет-соединение и доступность API.

### Темы не поднимаются автоматически

1. Проверьте `enable_auto_bump: true` в config.json
2. Сделайте первое поднятие вручную через 🚀
3. Проверьте логи в bot.log

## 👤 Автор

[QIYANA](https://lolz.live/kqlol/) - создатель бота

---

**Приятного использования! 🚀**
