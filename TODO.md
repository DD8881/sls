# TODO

## Оновлення даних

### Ручний прогон (швидко, з терміналу)

```bash
# 1. Скрап усіх мереж/міст у discounts.db (8 мереж, ~10 паралельних воркерів)
python3 run_scraper.py
#    опційно вужче:  --city Київ   --chain silpo   --workers 5

# 2. Перегенерувати статику й задеплоїти Worker
./deploy.sh
```

SQLite (`discounts.db`) живе лише локально як build-time проміжний шар; на
хостингу БД немає (див. DEPLOY.md).

### [x] Автоматично щодня через launchd (реалізовано 2026-06-27)

Джоба `com.sls.refresh` ганяє повний цикл `run_scraper.py && deploy.sh` щодня
о **13:00** без участі людини.

Файли:
- `scripts/refresh.sh` — обгортка: `disablesleep`-тогл → `caffeinate -i -s` →
  scrape → deploy. `trap … EXIT INT TERM` ЗАВЖДИ повертає `disablesleep 0`
  (навіть на краші/кіллі), лог у `~/Library/Logs/sls-refresh.log`, macOS-
  нотифікація на фейл.
- `~/Library/LaunchAgents/com.sls.refresh.plist` — `StartCalendarInterval`
  13:00; `SoftResourceLimits/NumberOfFiles = 8192` (див. нижче).
- `scripts/sls-pmset.sudoers` → `/etc/sudoers.d/sls-pmset` — вузьке NOPASSWD
  лише на `pmset -a disablesleep 0|1`, щоб джоба тоглила сон без пароля.

Налаштування сну/пробудження (три кейси о 13:00):
1. спить, кришка закрита, **батарея** → `pmset repeat wakeorpoweron MTWRFSU
   12:58:00` будить, а `disablesleep 1` тримає попри закриту кришку (caffeinate
   lid-event НЕ перебиває). **Тільки в зарядці надійно за тривалий прогон.**
2. кришка відкрита, працюю на батареї → `caffeinate -i -s` не дає idle-sleep.
3. цикл завершився → `trap` знімає `disablesleep` → Mac засинає нормально.

Якщо Mac спав і пробудження не відпрацювало — launchd доганяє пропущений 13:00
при наступному відкритті кришки.

Керування:
```bash
launchctl kickstart -k gui/$(id -u)/com.sls.refresh   # запустити негайно
launchctl print gui/$(id -u)/com.sls.refresh | grep -E "state|last exit"
launchctl bootout/bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sls.refresh.plist
tail -f ~/Library/Logs/sls-refresh.log
```

Одноразовий сетап (sudo, у реальному терміналі — не через `!`):
```bash
sudo install -m 0440 -o root -g wheel scripts/sls-pmset.sudoers /etc/sudoers.d/sls-pmset
sudo pmset repeat wakeorpoweron MTWRFSU 12:58:00
```

## [ ] Уроки першого бойового прогону (2026-06-27, ~2 год 11 хв)

- **launchd ліміт дескрипторів = 256** (у терміналі 1 048 576). Varus 3-рівнева
  паралелізація (~250 сокетів) валилась у `Too many open files` (errno 24) +
  каскад DNS (953 фейли). Виправлено `SoftResourceLimits/NumberOfFiles = 8192`
  у plist. УВАГА: цей ліміт б'є ЛИШЕ під launchd — ручний прогон його не бачить.
- **Varus 500-ки при workers=10** (272 шт.) — окрема проблема перевантаження
  його GraphQL, не повʼязана з дескрипторами. Розглянути зниження воркерів саме
  для Varus (нотатка в памʼяті: тримати ≤5) або per-chain workers у run_scraper.
- Тайминг: scrape ~2 год 05 хв (Silpo 442 маг. — головний споживач, ~1 год),
  deploy ~6 хв (7847 файлів). Тобто 13:00-джоба завершується ближче до ~15:10.

## [ ] Більше магазинів Silpo для Києва

(зараз скрап бере всі 442 branch'і Silpo по Україні; питання радше про
повноту/актуальність окремих міст)
