#!/bin/bash

# Скрипт для деплоя Telegram Mirror Bot на Ubuntu сервер
# Использование: ./deploy.sh user@server_ip

set -e

if [ -z "$1" ]; then
    echo "Использование: ./deploy.sh user@server_ip"
    echo "Пример: ./deploy.sh ubuntu@192.168.1.100"
    exit 1
fi

SERVER=$1
REMOTE_DIR="/home/$(echo $SERVER | cut -d'@' -f1)/tgparsnurxa"
REPO_URL="https://github.com/cnvncd/TGpars.git"

echo "🚀 Начинаем деплой на $SERVER..."

# Проверка SSH подключения
echo "📡 Проверка SSH подключения..."
ssh -o ConnectTimeout=5 $SERVER "echo 'SSH подключение успешно'" || {
    echo "❌ Ошибка: не удалось подключиться к серверу"
    echo "Убедитесь, что:"
    echo "  1. SSH ключ добавлен на сервер"
    echo "  2. Сервер доступен"
    echo "  3. Правильный формат: user@ip"
    exit 1
}

# Проверка существования директории
echo "📁 Проверка директории проекта..."
ssh $SERVER "[ -d $REMOTE_DIR ] && echo 'exists' || echo 'not_exists'" | grep -q "exists" && {
    echo "📦 Обновление существующего проекта..."
    
    # Обновление кода
    ssh $SERVER "cd $REMOTE_DIR && git pull origin main"
    
    # Обновление зависимостей
    ssh $SERVER "cd $REMOTE_DIR && source venv/bin/activate && pip install -r requirements.txt"
    
    # Перезапуск сервиса
    echo "🔄 Перезапуск сервиса..."
    ssh $SERVER "sudo systemctl restart telegram-mirror"
    
} || {
    echo "❌ Проект не найден на сервере!"
    echo "Сначала выполните первоначальную настройку:"
    echo "  ./setup_server.sh $SERVER"
    exit 1
}

# Проверка статуса сервиса
echo "✅ Проверка статуса сервиса..."
ssh $SERVER "sudo systemctl status telegram-mirror --no-pager" || {
    echo "⚠️  Сервис не запущен. Проверьте логи:"
    echo "  ssh $SERVER 'sudo journalctl -u telegram-mirror -n 50'"
    exit 1
}

echo "✅ Деплой завершен успешно!"
echo ""
echo "Полезные команды:"
echo "  Логи:      ssh $SERVER 'sudo journalctl -u telegram-mirror -f'"
echo "  Статус:    ssh $SERVER 'sudo systemctl status telegram-mirror'"
echo "  Рестарт:   ssh $SERVER 'sudo systemctl restart telegram-mirror'"
echo "  Стоп:      ssh $SERVER 'sudo systemctl stop telegram-mirror'"
