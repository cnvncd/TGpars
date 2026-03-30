# Инструкция по деплою на Ubuntu сервер

## Подготовка SSH ключа

Для автоматического деплоя нужно настроить SSH доступ по ключу.

### 1. Генерация SSH ключа (если еще нет)

На вашем компьютере выполните:

```bash
ssh-keygen -t ed25519 -C "telegram-mirror-deploy"
```

Нажмите Enter для сохранения в стандартное место (~/.ssh/id_ed25519)

### 2. Копирование ключа на сервер

**Автоматический способ:**
```bash
ssh-copy-id user@server_ip
```

**Ручной способ:**
```bash
cat ~/.ssh/id_ed25519.pub | ssh user@server_ip 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'
```

**Для Windows (PowerShell):**
```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh user@server_ip "cat >> ~/.ssh/authorized_keys"
```

### 3. Проверка подключения

```bash
ssh user@server_ip
```

Если подключение прошло без запроса пароля - всё готово!

---

## Первоначальная настройка сервера

### 1. Запуск скрипта настройки

```bash
chmod +x setup_server.sh
./setup_server.sh user@server_ip
```

Скрипт выполнит:
- Установку Python 3, pip, venv, git
- Клонирование репозитория в `/home/user/tgparsnurxa`
- Создание виртуального окружения
- Установку зависимостей
- Настройку .env файла (интерактивно)
- Настройку systemd сервиса
- Первый запуск для авторизации в Telegram

### 2. Данные для настройки

Подготовьте заранее:

- **API_ID** и **API_HASH** - получите на https://my.telegram.org/apps
- **SOURCE_CHANNELS** - ID или username каналов-источников (через запятую)
  - Пример: `-1001234567890,@channel_username`
  - Получить ID: перешлите сообщение из канала боту @userinfobot
- **TARGET_CHANNEL** - ID или username целевого канала
  - Пример: `-1009876543210`
- **MY_LINK** - ваша ссылка для замены всех кликабельных элементов
  - Пример: `https://example.com/`

### 3. Авторизация в Telegram

При первом запуске бот попросит:
1. Номер телефона (в международном формате, например: +1234567890)
2. Код подтверждения из Telegram
3. Пароль 2FA (если включен)

После успешной авторизации создастся файл `mirror_session.session`

---

## Обновление кода (деплой)

После внесения изменений в код:

### 1. Закоммитьте и запушьте изменения

```bash
git add .
git commit -m "Описание изменений"
git push origin main
```

### 2. Запустите деплой

```bash
chmod +x deploy.sh
./deploy.sh user@server_ip
```

Скрипт выполнит:
- Подключение к серверу
- `git pull` для получения последних изменений
- Обновление зависимостей
- Перезапуск сервиса

---

## Управление сервисом

### Просмотр логов в реальном времени

```bash
ssh user@server_ip 'sudo journalctl -u telegram-mirror -f'
```

### Просмотр последних 50 строк логов

```bash
ssh user@server_ip 'sudo journalctl -u telegram-mirror -n 50'
```

### Проверка статуса

```bash
ssh user@server_ip 'sudo systemctl status telegram-mirror'
```

### Перезапуск сервиса

```bash
ssh user@server_ip 'sudo systemctl restart telegram-mirror'
```

### Остановка сервиса

```bash
ssh user@server_ip 'sudo systemctl stop telegram-mirror'
```

### Запуск сервиса

```bash
ssh user@server_ip 'sudo systemctl start telegram-mirror'
```

### Отключение автозапуска

```bash
ssh user@server_ip 'sudo systemctl disable telegram-mirror'
```

---

## Структура на сервере

```
/home/user/tgparsnurxa/
├── mirror_userbot.py           # Основной скрипт
├── requirements.txt            # Зависимости
├── .env                        # Конфигурация (создается при setup)
├── .gitignore                  # Git исключения
├── venv/                       # Виртуальное окружение
├── mappings.db                 # База данных маппингов
├── mirror_session.session      # Сессия Telegram
└── log.txt                     # Логи бота
```

---

## Troubleshooting

### Ошибка: "SSH подключение не удалось"

1. Проверьте, что сервер доступен: `ping server_ip`
2. Проверьте SSH порт (по умолчанию 22): `ssh -p 22 user@server_ip`
3. Убедитесь, что SSH ключ добавлен на сервер

### Ошибка: "Permission denied"

```bash
ssh user@server_ip 'chmod 600 ~/.ssh/authorized_keys'
ssh user@server_ip 'chmod 700 ~/.ssh'
```

### Сервис не запускается

Проверьте логи:
```bash
ssh user@server_ip 'sudo journalctl -u telegram-mirror -n 100'
```

Проверьте .env файл:
```bash
ssh user@server_ip 'cat ~/tgparsnurxa/.env'
```

### FloodWaitError

Telegram ограничивает частоту запросов. Бот автоматически ждет и повторяет запрос.

### Сессия не сохраняется

Проверьте права на файл:
```bash
ssh user@server_ip 'ls -la ~/tgparsnurxa/*.session'
```

Если нужно, дайте права:
```bash
ssh user@server_ip 'chmod 600 ~/tgparsnurxa/*.session'
```

---

## Безопасность

⚠️ **ВАЖНО:**

1. Никогда не коммитьте `.env` файл в git
2. Никогда не коммитьте `.session` файлы
3. Храните API_HASH в секрете
4. Используйте отдельный Telegram аккаунт для бота (не основной)
5. Регулярно обновляйте систему: `ssh user@server_ip 'sudo apt update && sudo apt upgrade'`

---

## Быстрый старт

```bash
# 1. Настройка SSH ключа
ssh-copy-id user@server_ip

# 2. Первоначальная настройка
chmod +x setup_server.sh
./setup_server.sh user@server_ip

# 3. Проверка работы
ssh user@server_ip 'sudo systemctl status telegram-mirror'

# 4. Просмотр логов
ssh user@server_ip 'sudo journalctl -u telegram-mirror -f'

# 5. При обновлении кода
git push origin main
chmod +x deploy.sh
./deploy.sh user@server_ip
```

---

## Полезные команды

### Просмотр использования ресурсов

```bash
ssh user@server_ip 'top -b -n 1 | grep python'
```

### Очистка старых логов

```bash
ssh user@server_ip 'sudo journalctl --vacuum-time=7d'
```

### Бэкап базы данных

```bash
scp user@server_ip:~/tgparsnurxa/mappings.db ./mappings_backup_$(date +%Y%m%d).db
```

### Восстановление базы данных

```bash
scp ./mappings_backup.db user@server_ip:~/tgparsnurxa/mappings.db
```
