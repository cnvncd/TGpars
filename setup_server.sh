#!/bin/bash

# Скрипт первоначальной настройки сервера для Telegram Mirror Bot
# Использование: ./setup_server.sh user@server_ip

set -e

if [ -z "$1" ]; then
    echo "Использование: ./setup_server.sh user@server_ip"
    echo "Пример: ./setup_server.sh ubuntu@192.168.1.100"
    exit 1
fi

SERVER=$1
USER=$(echo $SERVER | cut -d'@' -f1)
REMOTE_DIR="/home/$USER/tgparsnurxa"
REPO_URL="https://github.com/cnvncd/TGpars.git"

echo "🚀 Начинаем первоначальную настройку сервера $SERVER..."

# Проверка SSH подключения
echo "📡 Проверка SSH подключения..."
ssh -o ConnectTimeout=5 $SERVER "echo 'SSH подключение успешно'" || {
    echo "❌ Ошибка: не удалось подключиться к серверу"
    echo ""
    echo "Для настройки SSH ключа выполните на ВАШЕМ компьютере:"
    echo "  ssh-keygen -t ed25519 -C 'telegram-mirror-deploy'"
    echo "  ssh-copy-id $SERVER"
    echo ""
    echo "Или скопируйте ключ вручную:"
    echo "  cat ~/.ssh/id_ed25519.pub | ssh $SERVER 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'"
    exit 1
}

# Обновление системы и установка зависимостей
echo "📦 Установка системных зависимостей..."
ssh $SERVER "sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv git"

# Клонирование репозитория
echo "📥 Клонирование репозитория..."
ssh $SERVER "[ -d $REMOTE_DIR ] && echo 'Директория уже существует' || git clone $REPO_URL $REMOTE_DIR"

# Создание виртуального окружения
echo "🐍 Создание виртуального окружения..."
ssh $SERVER "cd $REMOTE_DIR && python3 -m venv venv"

# Установка зависимостей Python
echo "📚 Установка зависимостей Python..."
ssh $SERVER "cd $REMOTE_DIR && source venv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt"

# Создание .env файла
echo "⚙️  Настройка конфигурации..."
echo ""
echo "Теперь нужно настроить .env файл на сервере."
echo "Введите данные для конфигурации:"
echo ""

read -p "API_ID (из https://my.telegram.org/apps): " API_ID
read -p "API_HASH: " API_HASH
read -p "SOURCE_CHANNELS (через запятую, например: -1001234567890,@channel): " SOURCE_CHANNELS
read -p "TARGET_CHANNEL (например: -1009876543210): " TARGET_CHANNEL
read -p "MY_LINK (например: https://example.com/): " MY_LINK
read -p "ALBUM_DELAY_SECONDS (по умолчанию 2): " ALBUM_DELAY
ALBUM_DELAY=${ALBUM_DELAY:-2}

# Создание .env на сервере
ssh $SERVER "cat > $REMOTE_DIR/.env << EOF
API_ID=$API_ID
API_HASH=$API_HASH
SOURCE_CHANNELS=$SOURCE_CHANNELS
TARGET_CHANNEL=$TARGET_CHANNEL
MY_LINK=$MY_LINK
ALBUM_DELAY_SECONDS=$ALBUM_DELAY
EOF"

echo "✅ .env файл создан"

# Настройка systemd service
echo "🔧 Настройка systemd сервиса..."
ssh $SERVER "sudo bash -c 'cat > /etc/systemd/system/telegram-mirror.service << EOF
[Unit]
Description=Telegram Mirror Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REMOTE_DIR
Environment=\"PATH=$REMOTE_DIR/venv/bin\"
ExecStart=$REMOTE_DIR/venv/bin/python3 mirror_userbot.py
Restart=always
RestartSec=10
StandardOutput=append:$REMOTE_DIR/log.txt
StandardError=append:$REMOTE_DIR/log.txt

[Install]
WantedBy=multi-user.target
EOF'"

# Перезагрузка systemd
echo "🔄 Перезагрузка systemd..."
ssh $SERVER "sudo systemctl daemon-reload"

# Первый запуск для авторизации
echo ""
echo "📱 ВАЖНО: Первый запуск для авторизации в Telegram"
echo "Сейчас запустится бот и попросит ввести номер телефона и код."
echo ""
read -p "Нажмите Enter для продолжения..."

ssh -t $SERVER "cd $REMOTE_DIR && source venv/bin/activate && python3 mirror_userbot.py" || {
    echo ""
    echo "⚠️  Если авторизация прошла успешно (создан файл .session), это нормально."
}

# Включение и запуск сервиса
echo ""
echo "🚀 Запуск сервиса..."
ssh $SERVER "sudo systemctl enable telegram-mirror"
ssh $SERVER "sudo systemctl start telegram-mirror"

# Проверка статуса
echo "✅ Проверка статуса..."
ssh $SERVER "sudo systemctl status telegram-mirror --no-pager" || true

echo ""
echo "✅ Настройка завершена!"
echo ""
echo "Полезные команды:"
echo "  Логи:      ssh $SERVER 'sudo journalctl -u telegram-mirror -f'"
echo "  Статус:    ssh $SERVER 'sudo systemctl status telegram-mirror'"
echo "  Рестарт:   ssh $SERVER 'sudo systemctl restart telegram-mirror'"
echo "  Стоп:      ssh $SERVER 'sudo systemctl stop telegram-mirror'"
echo ""
echo "Для обновления кода используйте:"
echo "  ./deploy.sh $SERVER"
