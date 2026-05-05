#!/bin/bash

# Telegram Bot Monitor - Установщик на сервер
# Поддерживает Ubuntu/Debian/CentOS

set -e

echo "======================================"
echo "Telegram Bot Monitor - Установка"
echo "======================================"

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Проверка прав
if [ "$EUID" -eq 0 ]; then 
    echo -e "${YELLOW}Внимание: Запуск от root${NC}"
fi

# Определение ОС
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
        VER=$VERSION_ID
    else
        echo -e "${RED}Не удалось определить ОС${NC}"
        exit 1
    fi
}

# Установка зависимостей системы
install_system_deps() {
    echo -e "${GREEN}Установка системных зависимостей...${NC}"
    
    case $OS in
        ubuntu|debian)
            sudo apt-get update
            sudo apt-get install -y python3 python3-pip python3-venv git screen
            ;;
        centos|rhel|fedora)
            sudo yum install -y python3 python3-pip git screen
            ;;
        *)
            echo -e "${RED}Неподдерживаемая ОС${NC}"
            exit 1
            ;;
    esac
}

# Клонирование или создание директории
setup_project() {
    echo -e "${GREEN}Настройка проекта...${NC}"
    
    if [ -d "telegram-bot-monitor" ]; then
        echo -e "${YELLOW}Директория уже существует. Обновление...${NC}"
        cd telegram-bot-monitor
        git pull 2>/dev/null || echo "Обновление через git не выполнено"
    else
        mkdir -p telegram-bot-monitor
        cd telegram-bot-monitor
    fi
}

# Создание виртуального окружения
setup_venv() {
    echo -e "${GREEN}Создание виртуального окружения...${NC}"
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip
}

# Установка зависимостей Python
install_python_deps() {
    echo -e "${GREEN}Установка Python зависимостей...${NC}"
    
    cat > requirements.txt << EOF
aiogram==2.25.1
aiohttp==3.9.1
aiohttp-socks==0.8.4
python-dotenv==1.0.0
EOF
    
    pip install -r requirements.txt
}

# Создание скрипта запуска
create_start_script() {
    echo -e "${GREEN}Создание скриптов запуска...${NC}"
    
    # Скрипт запуска
    cat > start.sh << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
python3 bot.py
EOF
    chmod +x start.sh
    
    # Скрипт остановки
    cat > stop.sh << 'EOF'
#!/bin/bash
pkill -f "python3 bot.py"
echo "Бот остановлен"
EOF
    chmod +x stop.sh
    
    # Скрипт для управления через screen
    cat > screen-start.sh << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
screen -dmS bot-monitor python3 bot.py
echo "Бот запущен в screen сессии 'bot-monitor'"
echo "Для просмотра: screen -r bot-monitor"
echo "Для выхода: Ctrl+A, затем D"
EOF
    chmod +x screen-start.sh
    
    cat > screen-stop.sh << 'EOF'
#!/bin/bash
screen -S bot-monitor -X quit
echo "Бот остановлен"
EOF
    chmod +x screen-stop.sh
}

# Создание systemd сервиса
create_systemd_service() {
    echo -e "${GREEN}Создание systemd сервиса...${NC}"
    
    CURRENT_DIR=$(pwd)
    USER=$(whoami)
    
    sudo cat > /etc/systemd/system/bot-monitor.service << EOF
[Unit]
Description=Telegram Bot Monitor
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$CURRENT_DIR
ExecStart=$CURRENT_DIR/venv/bin/python3 $CURRENT_DIR/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    echo -e "${GREEN}Сервис создан. Используйте:${NC}"
    echo "  sudo systemctl start bot-monitor   # Запуск"
    echo "  sudo systemctl stop bot-monitor    # Остановка"
    echo "  sudo systemctl status bot-monitor  # Статус"
    echo "  sudo systemctl enable bot-monitor  # Автозапуск"
}

# Создание конфигурации прокси по умолчанию
create_proxy_config() {
    if [ ! -f "proxy_config.json" ]; then
        cat > proxy_config.json << 'EOF'
{
    "proxy_url": "",
    "description": "Форматы прокси:\n- HTTP: http://username:password@host:port или http://host:port\n- HTTPS: https://username:password@host:port\n- SOCKS5: socks5://username:password@host:port",
    "examples": [
        "http://user:pass@192.168.1.1:8080",
        "socks5://user:pass@127.0.0.1:9050",
        "http://proxy.example.com:3128"
    ]
}
EOF
        echo -e "${GREEN}Файл proxy_config.json создан${NC}"
    fi
}

# Создание README
create_readme() {
    cat > README.md << 'EOF'
# Telegram Bot Monitor

Бот для мониторинга API услуг с уведомлениями об изменениях.

## Установка

1. Клонируйте репозиторий:
```bash
git clone <repository-url>
cd telegram-bot-monitor
