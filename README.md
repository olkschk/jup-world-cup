# Jupiter World Cup — Freeroll Bot

Автоматизированный скрипт для участия в [Jupiter Prediction Market World Cup Challenge](https://jup.ag/prediction/world-cup).

## Что делает

Для каждого кошелька из списка:

1. Проверяет баланс SOL (минимум `MIN_SOL_BALANCE`)
2. Применяет реферальный код (с подписью кошелька)
3. Выбирает 5 случайных матчей — фаворита в каждом (наименьший коэффициент; матчи, где хоть одна команда ≥ 70%, пропускаются)
4. Получает unsigned Solana-транзакцию, подписывает локально
5. Отправляет freeroll-ставку
6. Сохраняет статус в SQLite-базе

## Требования

- Python 3.10+
- MongoDB **не нужен** — используется встроенный SQLite

```bash
pip install -r requirements.txt
```

## Структура проекта

```
├── data/
│   ├── seeds.txt        # seed-фразы (приоритет), по одной на строку
│   ├── privatekeys.txt  # base58 приватные ключи (fallback, если seeds.txt пуст)
│   └── proxies.txt      # прокси (необязательно), по одному на строку
├── config.py            # настройки из .env
├── crypto_utils.py      # Fernet-шифрование приватных ключей
├── db.py                # SQLite: статусы кошельков
├── wallet.py            # деривация keypair, баланс SOL
├── jupiter_api.py       # API-клиент Jupiter Prediction Market
├── init_wallets.py      # логика инициализации кошельков
└── main.py              # CLI: init / run / stats
```

## Настройка

```bash
cp .env.example .env
```

| Переменная | Описание | По умолчанию |
|---|---|---|
| `DB_FILE` | Имя SQLite-файла в папке `data/` | `wallets.db` |
| `SOLANA_RPC` | RPC endpoint Solana | mainnet-beta |
| `FERNET_KEY` | Ключ шифрования приватных ключей | **обязательно** |
| `REFERRAL_CODE` | Реферальный код для привязки | пусто (пропускается) |
| `MIN_SOL_BALANCE` | Минимальный баланс SOL | `0.001` |
| `MAX_WORKERS` | Параллельных кошельков (только при наличии прокси) | `3` |

Генерация `FERNET_KEY`:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Формат файлов `data/`

**seeds.txt** — одна seed-фраза (12 или 24 слова) на строку:
```
word1 word2 word3 ... word12
```

**privatekeys.txt** — один base58 приватный ключ на строку:
```
5Jxxx...
```

**proxies.txt** — поддерживаемые форматы:
```
ip:port
ip:port:login:password
http://ip:port
http://ip:port:login:password
http://login:password@ip:port
socks5://login:password@ip:port
```

## Запуск

### Инициализация кошельков (один раз)
```bash
python main.py init
```
Читает `seeds.txt` (приоритет) или `privatekeys.txt`, деривирует keypair, шифрует и сохраняет в БД. Уже существующие кошельки пропускаются.

### Запуск ставок
```bash
python main.py run
```

### Статистика
```bash
python main.py stats
```

Пример вывода:
```
============================================================
  STATISTICS
============================================================
  Total wallets :    25

  DONE                   18  (72.0%)
  PENDING                 3  (12.0%)
  LOW BALANCE             3  (12.0%)
  ERROR: ...              1  ( 4.0%)

  Referral applied :     15  (60.0%)
============================================================

  LOW BALANCE wallets (3):
  Address                                         Balance
  ------------------------------------------------------
  AbCd1234...XyZz                                 0.000000 SOL
  EfGh5678...WvUt                                 0.000891 SOL
```

## Многопоточность

| Условие | Режим |
|---|---|
| `proxies.txt` пуст / отсутствует | Последовательно, все кошельки по очереди |
| `proxies.txt` содержит прокси | Параллельно, до `MAX_WORKERS` кошельков одновременно |

Прокси назначаются кошелькам round-robin. Случайные задержки 3–10 с между каждым действием применяются в обоих режимах.

## Статусы в БД

| Статус | Описание |
|---|---|
| `PENDING` | Ещё не обработан |
| `DONE` | Ставка успешно размещена |
| `LOW BALANCE` | Недостаточно SOL |
| `ERROR: <детали>` | Ошибка с деталями |

При повторном запуске `run`: `DONE` — пропускается; `LOW BALANCE` и `ERROR` — повторяются.
