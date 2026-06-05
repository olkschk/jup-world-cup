# Jupiter World Cup — Freeroll Bot

Автоматизированный скрипт для участия в Jupiter Prediction Market World Cup Challenge.

## Что делает

Для каждого кошелька из списка:

1. Проверяет баланс SOL (минимум `MIN_SOL_BALANCE`)
2. Применяет реферальный код (с подписью кошелька)
3. Выбирает 5 случайных матчей — фаворита в каждом (минимальный коэффициент, без матчей где хоть одна команда ≥ 70%)
4. Получает unsigned Solana транзакцию, подписывает её локально
5. Отправляет freeroll ставку
6. Сохраняет статус в MongoDB

## Структура

```
├── data/
│   ├── seeds.txt        # seed-фразы (приоритет), по одной на строку
│   ├── privatekeys.txt  # base58 приватные ключи (fallback если seeds.txt пуст)
│   └── proxies.txt      # прокси (необязательно), по одному на строку
├── config.py            # все настройки из .env
├── crypto_utils.py      # Fernet-шифрование приватных ключей
├── db.py                # MongoDB: статусы кошельков
├── wallet.py            # деривация keypair, баланс SOL
├── jupiter_api.py       # API-клиент Jupiter Prediction Market
├── init_wallets.py      # разовая инициализация: seed/key → MongoDB
└── main.py              # основной runner
```

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

```bash
cp .env.example .env
```

Заполни `.env`:

| Переменная | Описание |
|---|---|
| `MONGO_URI` | URI MongoDB (default: `mongodb://localhost:27017`) |
| `FERNET_KEY` | Ключ шифрования приватных ключей (обязательно) |
| `SOLANA_RPC` | RPC endpoint Solana |
| `REFERRAL_CODE` | Реферальный код для привязки (опционально) |
| `MIN_SOL_BALANCE` | Минимальный баланс SOL (default: `0.001`) |
| `MAX_WORKERS` | Кол-во параллельных кошельков (default: `3`, только при наличии прокси) |

Генерация `FERNET_KEY`:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Формат файлов в `data/`

**seeds.txt** — одна seed-фраза на строку:
```
word1 word2 word3 ... word12
word1 word2 word3 ... word12
```

**privatekeys.txt** — один base58 приватный ключ на строку:
```
5Jxxx...
4Kyyy...
```

**proxies.txt** — один прокси на строку:
```
http://user:pass@host:port
http://host:port
socks5://user:pass@host:port
```

## Запуск

```bash
# 1. Инициализация кошельков (один раз)
python init_wallets.py

# 2. Запуск ставок
python main.py
```

## Статусы кошельков в MongoDB

| Статус | Описание |
|---|---|
| `PENDING` | Ожидает обработки |
| `DONE` | Ставка успешно размещена |
| `LOW BALANCE` | Недостаточно SOL |
| `ERROR: ...` | Ошибка с деталями |

При повторном запуске: `DONE` — пропускается, `LOW BALANCE` и `ERROR` — повторяются.

## Многопоточность

Если `data/proxies.txt` содержит прокси — кошельки обрабатываются параллельно (до `MAX_WORKERS` одновременно), каждый через свой прокси. Без прокси — строго последовательно.
