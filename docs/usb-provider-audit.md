# Офлайн-аудит USB/provider-плана rodin

Статус: **host-only, кандидат не загружался на телефоне**. Проверены OTA DTB,
инвентарь PLATFORM-модулей и сохранённые kernel config/Android rc. Это не
проверка пробинга, ABI, принятия AVB или работающего USB console.

## Методология и воспроизведение

`tools/inspect_dtb.py` читает ровно один FDT v17, непосредственно либо после
64-байтной MTK-обёртки. Проверяет размеры, пересечения блоков, reservation map,
токены, вложенность, имена и phandle. Выдаёт дерево, compatible-index и карту
phandle. Свойства имеют `type`, `value`, `hex`: FDT не содержит типов, поэтому
отображение ASCII-строк/списков, u32 и байтов — эвристика; исходные байты
сохранены в hex. Пустое свойство отображается как пустой массив байтов.
Конкатенации DTB и хвост после totalsize намеренно отвергаются.

```sh
python3 tools/inspect_dtb.py --dtb /tmp/input.dtb > /tmp/dt.json
python3 tools/check_dt_modules.py --dump /tmp/dt.json \
  --inventory /path/to/build/module-inventory.json \
  --config /path/to/build/inspection/ota-kernel.config
# Вместо --dump можно передать --dtb /tmp/input.dtb.
```

Срез входа берётся из `build/ota/vendor_boot.img` по `dtb_offset/dtb_size`
из `build/inspection/ota-vendor_boot.json`; основной репозиторий читается
без записи. Бинарные срезы и JSON-дампы — временные, не часть Git.
Инструменты используют только stdlib Python 3, без сети и dtc.

`check_dt_modules.py` по умолчанию выбирает 12 нод заданного USB-пути;
`--node /path` позволяет проверять произвольные пути и явно обнаруживать
отсутствующие ноды. OF-alias из инвентаря имеют пары
`of:N*T*Ccompatible` / `of:N*T*CcompatibleC*`. Проверяются wildcard-ограничения
имени (без unit address), device_type и каждый compatible, включая fallback.
Это сопоставление OF-таблиц, а не доказательство загрузки через modalias.
Несколько модулей дают ambiguous, отсутствие совпадения — unresolved.

Специфические built-in-кандидаты допускаются только при соответствующем
`CONFIG=y`, всегда помечаются `builtin_heuristic`. Generic `syscon`/fixed-clock
не доказывают наличие SoC clock/power драйвера. Ресурсные PHY-подноды без
compatible связываются с владельцем родителя как `parent_provider_heuristic`.
Их создание нужно подтвердить исходниками. Status ноды выводится отдельно;
наличие владельца не означает, что нода включена или успешно пробилась.

Ссылки clocks/power-domains/phys разбираются с учётом `#*-cells` провайдера,
а не как список произвольных phandle. Для supply используется один phandle;
для vendor `mediatek,syscon-wakeup` подтверждается только первый phandle,
оставшиеся ячейки сохраняются без утверждения о binding. `tcpc` допускает
phandle или уникальное имя через свойство `tcpc,name`. Разрешённая ссылка на
ноду и наличие драйвера этой ноды — разные статусы.

## Перепроверка исходных фактов в этой сессии

| Факт | Результат |
| --- | --- |
| DTB offset 34922496, size 444841 в OTA vendor_boot | Перепроверено: значения inspection JSON, по ним прочитан срез; разбор выполнен `tools/inspect_dtb.py` в этой сессии. |
| MTK magic 0xd7b7ab1e и 64 байта перед FDT | Перепроверено `tools/inspect_dtb.py` в этой сессии: первые байты `d7b7ab1e` (big-endian представление), FDT начинается на +64. Hex-префикс обёртки: `d7b7ab1e0006c9a90000002000000020`; CLI сохраняет все 64 байта в `wrapper_hex`. |
| Ровно один FDT, version 17, totalsize 444777 | Перепроверено `tools/inspect_dtb.py` в этой сессии: 64 + 444777 = 444841, хвоста нет; last_comp_version=16. Дерево содержит 1989 нод. |
| usb0 compatible, OTG, role-switch, High-Speed | Перепроверено `tools/inspect_dtb.py` в этой сессии: `/soc/usb0@11201000`, compatible `mediatek,mt6899-mtu3`, `mediatek,mtu3`; `dr_mode=otg`, пустое `usb-role-switch`, `maximum-speed=high-speed`. |
| usb-phy0 с USB2/USB3-поднодами | Перепроверено `tools/inspect_dtb.py` в этой сессии: `/soc/usb-phy0@11c90000`, подноды `usb2-phy0@11c90000`, `usb3-phy0@11c93000`; у поднод нет compatible, есть `#phy-cells=1`. |
| `/soc/xhci0@11200000` | Исправлено `tools/inspect_dtb.py` в этой сессии: полный путь **`/soc/usb0@11201000/xhci0@11200000`**, compatible `mediatek,mtk-xhci`, status=okay. |
| extcon-usb, rt-pd-manager, charger, usb-boost-manager, usb-meta, typec-mux-switch, usb-dp-selector | Перепроверено `tools/inspect_dtb.py` в этой сессии: все присутствуют по путям таблицы ниже. `/extcon-usb` содержит строку `tcpc=type_c_port0`, не phandle. Имя уникально разрешается в MT6375 TCPC. |
| Fallback `16751000.usb0` из rc отсутствует в DT | Перепроверено `tools/inspect_dtb.py` в этой сессии: среди всех нод нет unit address 16751000. Это не разрешает использовать fallback для кандидата. |
| dtbo отсутствует в OTA payload | Перепроверено отдельно stdlib-чтением `payload.bin` из локального OTA ZIP: в protobuf manifest 37 разделов, dtbo нет. Не выводится из extraction.json: тот перечисляет только 5 выбранных разделов. Не доказывает отсутствие live dtbo или иной обработки DT загрузчиком. |
| 10 USB-модулей, кроме tcpci_late_sync, имеют совпадение OF-alias↔DT | Перепроверено `check_dt_modules.py` поверх дампа `inspect_dtb.py`: 9 из 10 в указанной ниже группе совпали; у tcpci_late_sync OF-alias нет. Это не все потенциальные драйверы USB-пути: у mux_switch и usb_dp_selector также отсутствуют aliases. |

Группа десяти: phy_mtk_xsphy, mtu3, xhci_mtk_hcd_v2, tcpc_mt6375,
extcon_mtk_usb, rt_pd_manager, mtk_charger_framework, usb_boost, usb_meta,
tcpci_late_sync. TCPC — дополнительная нода провайдера за пределами 12
целевых нод. У tcpc_class aliases также нет; это инфраструктурный модуль,
не отдельный OF-владелец в данной сверке.

## Владельцы USB-пути

| Нода | Офлайн-кандидат | Основание |
| --- | --- | --- |
| `/soc/usb0@11201000` | mtu3 | OF-alias для fallback `mediatek,mtu3` |
| `/soc/usb0@11201000/xhci0@11200000` | xhci_mtk_hcd_v2 | OF-alias |
| `/soc/usb-phy0@11c90000` | phy_mtk_xsphy | OF-alias |
| `/soc/usb-phy0@11c90000/usb2-phy0@11c90000` | phy_mtk_xsphy через родителя | Эвристика ресурсной PHY-ноды, не собственный alias |
| `/soc/usb-phy0@11c90000/usb3-phy0@11c93000` | phy_mtk_xsphy через родителя | Та же эвристика |
| `/extcon-usb` | extcon_mtk_usb | OF-alias |
| `/rt-pd-manager` | rt_pd_manager | OF-alias |
| `/charger` | mtk_charger_framework | OF-alias |
| `/usb-boost-manager` | usb_boost | OF-alias `mediatek,usb-boost` |
| `/usb-meta` | usb_meta | OF-alias |
| `/soc/typec-mux-switch` | **unresolved**; предполагается mux_switch | В инвентаре модуль есть, aliases нет; config не даёт специфического CONFIG=y основания |
| `/soc/usb-dp-selector@10005600` | **unresolved**; предполагается usb_dp_selector | В инвентаре модуль есть, aliases нет; config не даёт специфического CONFIG=y основания |

Итого **12 нод: 8 прямых module-кандидатов + 2 родительские эвристики,
2 unresolved**. Критерий «ноль безхозяинных USB-нод» не выполнен.
Отсутствие alias не означает отсутствие binding: драйвер может регистрировать
OF-таблицу без MODULE_DEVICE_TABLE или создавать устройства иначе.
Не следует закрывать эти две строки совпадением имён файлов.

## Provider-ссылки

Все запрошенные присутствующие ссылки разрешаются структурно:

- usb0 `vusb33-supply` → phandle 343,
  `/spmi@1cc04000/second_pmic@5/regulators/vusb` (mt6368_vusb).
  Прямой владелец regulator-подноды по OF-alias **unresolved**; нужен разбор
  parent PMIC/regulator-драйвера, не предположение о CONFIG_MFD_CORE.
- usb0 `clocks` → `/syscon@10000000` (91, аргументы 39 и 40),
  `/syscon@11036000` (88, аргумент 40). `CONFIG_MFD_SYSCON=y` даёт только
  syscon-эвристику, не доказательство регистрации clock IDs.
- usb0 `power-domains` → `/power-controller@1c001000` (87, домен 2).
  Его syscon-compatible даёт ту же ограниченную эвристику, не доказательство
  работающего genpd-провайдера.
- usb0 `phys` → USB2-поднода (337, аргумент 3) и USB3-поднода
  (344, аргумент 4); оба `#phy-cells=1`. Родительский драйвер — XSPHY-кандидат.
- xHCI и обе PHY-подноды `clocks` → `/clocks/clk26m` (320, без аргументов);
  fixed-clock + `CONFIG_COMMON_CLK=y` — built-in-эвристика.
- usb0 `mediatek,syscon-wakeup` → `/syscon@11036000` (88), далее 656, 105;
  интерпретация этих двух vendor-аргументов ожидает исходников.
- extcon `tcpc=type_c_port0` → `/soc/i2c@11f01000/mt6375@34/tcpc`,
  compatible `mediatek,mt6375-tcpc` → tcpc_mt6375 по OF-alias.

Структурно разрешённые ссылки не закрывают provider-драйверы, питание или
порядок пробинга; таблица готовности выше считает только целевые USB-ноды.

## Аннотация Android rc и отличие кандидата

Read-only `build/inspection/init.mt6899.usb.rc`:

1. `on post-fs`: создаёт `/config/usb_gadget/g1`, задаёт VID/bcdDevice/bcdUSB,
   OS descriptors, строки, набор функций (включая четыре ACM gs0–gs3),
   `configs/b.1`, строки конфигурации, MaxPower и ссылку OS descriptors.
   Монтирует FunctionFS для adb/mtp/ptp, задаёт параметры RNDIS/NCM/UVC,
   права; в конце `vendor.usb.functions.ready=1`.
2. `on boot`: права и параметры USB/TTY; задаёт
   `sys.usb.controller=${vendor.usb.controller:-16751000.usb0}`, затем права
   контроллера/Type-C и **`sys.usb.configfs=2`**. Это текст rc, не доказательство
   того, какой controller был выбран на устройстве.
3. Видимая `on charger` ветка создаёт урезанный g1 и ставит configfs=1.
   Обработчик `sys.usb.config=midi && sys.usb.configfs=1` задаёт PID/строку,
   делает symlink функции в конфигурацию, затем пишет **`${sys.usb.controller}`
   в `g1/UDC`**, после чего обновляет sys.usb.state. Нельзя приписывать этому
   обработчику configfs=2: полная обычная HAL/property-последовательность
   одним этим rc не доказана.

`initramfs/usb-init` не запускает Android init/HAL/property service. После
проверки хешей и загрузки модулей монтирует свой configfs в `/run/configfs`,
ждёт ровно один UDC из `/sys/class/udc` (до 30 секунд, неоднозначность — отказ),
создаёт `/usb_gadget/rodin`, свои диагностические VID/PID и строки, **один
`acm.usb0`**, конфигурацию c.1 и MaxPower=100. Делает symlink ACM, затем bind
обнаруженным именем в UDC; номера ttyGS0 берёт из sysfs. FunctionFS, ADB и
Android USB service отсутствуют. Эта последовательность проверена чтением,
не запускалась на телефоне. Экспериментальная оболочка — root без пароля;
thermal/power userspace не валидирован.

## Вопросы к исходникам, ожидают клонирования

Клонирование в этом треке не выполнялось; сеть не использовалась.

- Какова дефолтная peripheral-роль mtu3 при `dr_mode=otg` и usb-role-switch,
  если событие extcon/Type-C не приходит? Кто запускает роль/UDC?
- Нужны ли mtk_charger_framework и сам extcon_mtk_usb для peripheral ACM?
  Load-time зависимость extcon на tcpc_class, charger_class,
  mtk_charger_framework **есть в инвентаре**; необходимость extcon для этого
  сценария ещё не доказана. Нельзя удалять load-time зависимости из плана
  или обходить проверки ABI.
- Что tcpc_mt6375 требует от parent-MFD `/soc/i2c@11f01000/mt6375@34`
  (regmap, IRQ, питание, дочерняя регистрация)? OF совпадение этого не решает.
- Классифицировать usb_boost, usb_dp_selector, usb_meta, tcpci_late_sync как
  обязательные для gadget или лишние. Отдельно установить binding
  mux_switch/usb_dp_selector без опубликованных OF-alias.
- Где применяется `maximum-speed=high-speed` в vendor mtu3: DT задаёт
  верхний предел gadget **High-Speed**, не SuperSpeed; проверить соблюдение
  этого ограничения драйвером и фактически согласованную скорость.

## Вердикт

| Доказано офлайн | Недоказуемо офлайн — нужен аппаратный эксперимент |
| --- | --- |
| Структура и свойства одного OTA FDT, отсутствие dtbo в этом payload | Фактический DT после правок загрузчиком; отсутствие live dtbo/оверлеев |
| 9/10 совпадений выбранной группы модулей с DT; 8 прямых владельцев целевых нод | Успешная загрузка, ABI/modversions, пробинг, регистрация regulator/clock/genpd |
| Разрешение phandle/TCPC-имени; явные owner-unresolved и эвристики | Реальные напряжения, clocks, power domains, PHY, Type-C/PD и peripheral-роль |
| Текст rc, configfs ACM-план кандидата, DT High-Speed limit | Появление UDC, bind ACM, enumeration, ttyGS0, передача данных и реальная скорость |
| Отсутствие оснований объявлять кандидат рабочим | Загрузка нативного Linux, принятие AVB-цепочкой, термальная/энергетическая безопасность |

До аппаратной стадии нужны ответы из исходников и отдельное согласование
короткого локального эксперимента с физически продемонстрированным recovery.
Никаких reboot, USB runtime изменений или прошивки в этой сессии не было.
