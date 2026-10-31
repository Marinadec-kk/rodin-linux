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

## Source-evidence: provenance и границы доказательства

Все ссылки ниже — относительно **основного read-only репозитория**
`/home/marinadec/projects/operation_unlock_poco/linux-rodin/`, не worktree.
Префиксы `K` = `build/src/xiaomi-kernel`, `V` =
`build/src/mt6897-modules`, `C` = `build/inspection/ota-kernel.config`.
Номера строк относятся к закреплённым локальным файлам. HEAD всех трёх
деревьев проверены `git rev-parse HEAD`; сеть в этом проходе не использовалась.

- [ТОЧНО: MiCode@2cd69ee] Xiaomi_Kernel_OpenSource:
  `2cd69ee4193920840ee3c837384716a66b1faecd`. Это точный закреплённый
  BSP-source input, но **не доказательство**, что поставляемый `mtu3.ko`
  собран именно из данного in-tree файла с теми же опциями/патчами.
- Точный отдельный vendor input mt6899-devs `vendor-mediatek-modules`:
  `26e64cd4c795b2ff2c07d530b956beabfdada2f1`; USB-реализаций здесь нет.
  Каталог `udc/` — compression, не USB Device Controller. Наличие этого
  дерева не закрывает отсутствующий device-modules source.
- [НЕДОСТУПНО: пропавший репо] Закреплённый
  `mt6899-devs/android_kernel_device_modules_6.6@ea65723` ранее вернул 404
  (входной факт этого прохода, не повторный сетевой запрос). Поэтому
  точные MT6899 vendor Kconfig/Makefile, stub/v2 реализации и соответствие
  поставляемым ELF здесь проверить нельзя.
- [ИНДИКАТИВНО: MT6897@68a7bfe] Замена:
  `WitAqua-Devices/kernel_xiaomi_mt6897-modules`, lineage-23.2,
  `68a7bfeec82a9c32a55417bf9d2708faf4b28284` (2026-09-03 по входному
  контексту). Другой SoC/ревизия; она объясняет семейство драйверов,
  но не подтверждает точный MT6899 ABI, поведение или имена built-in.
  В частности, отсутствие v2/stub/mt6765 в этой замене — существенный
  provenance-разрыв, не повод придумать CONFIG по похожему имени.

Для уточнения роли/compatible повторно прочитан OTA DTB через
`tools/inspect_dtb.py`: `role-switch-default-mode` и `enable-manual-drd`
у usb0 **отсутствуют**; compatible mux/selector точно с подчёркиваниями
`mediatek,typec_mux_switch` / `mediatek,usb_dp_selector`. Есть включённый
`/soc/i2c@11e00000/ps5170@28`, `parade,ps5170`. Дамп и бинарный срез
сохранены только в `/tmp`, не в Git. Ранее приведённые исторические факты
не являются новым наблюдением телефона.

## Q1. mtu3: роль, UDC, probe и скорость

[ТОЧНО: MiCode@2cd69ee]

- `K/drivers/usb/mtu3/mtu3_plat.c:385-421`: host-only/gadget-only CONFIG
  могут переопределить DT; в dual-role OTG начальное `is_host=true`,
  но `ssusb_gadget_init()` вызывается **до** host/switch init.
  `K/drivers/usb/mtu3/mtu3_dr.c:277-302` читает
  `role-switch-default-mode`: только `peripheral` даёт DEVICE, всё иное
  HOST; затем ставит эту роль через work. Чтение свойства:
  `K/drivers/usb/common/common.c:205-222`. На обследованном DT без
  default-mode это **HOST**, не автоматически peripheral.
  `USB_ROLE_NONE` тоже сводится к default, обычно HOST
  (`mtu3_dr.c:138-165`). Configfs bind сам роль не меняет.
- `K/drivers/usb/mtu3/mtu3_core.c:981-988` сначала останавливает device IP,
  затем регистрирует gadget; `mtu3_gadget.c:706-719` вызывает
  `usb_add_gadget_udc()`. UDC в OTG существует и допускает gadget bind,
  однако для физического peripheral-пути нужен DEVICE переход:
  `mtu3_dr.c:160-165` запускает device IP. В чистом peripheral
  `udc_start` сам вызывает `mtu3_start()` (`mtu3_gadget.c:530-541`).
  Регистрация UDC ≠ enumeration; последующая ошибка host/switch probe
  может откатить gadget (`mtu3_plat.c:415-424`).
- Имя UDC — имя parent platform device, **не** `g.name="mtu3"`:
  `K/drivers/usb/gadget/udc/core.c:1388-1392`;
  `K/drivers/of/platform.c:115-130` строит имя из переведённого reg-address
  и имени OF-ноды. Для usb0@11201000 ожидается **11201000.usb0**,
  согласуется с историческим sys.usb.controller, но новое появление sysfs
  на телефоне не проверено.
- При `usb-role-switch` probe обходит extcon-phandle lookup
  (`mtu3_plat.c:299-315`); прямых вызовов charger framework в этих
  in-tree файлах нет. **Нет прямого probe-gate extcon/charger** в этом
  source-пути. Это не отменяет функционального потребителя role-switch.
  Есть gates питания/clocks/PHY/MMIO/IRQ: `mtu3_plat.c:225-268,290-293`,
  `mtu3_core.c:933-978`, и общая hardware init. В OTG запрашивается vbus
  regulator; нельзя объявлять probe независимым от всех power providers.
- `maximum-speed` читается (`mtu3_core.c:957`), валидируется с допустимым
  HIGH (`:828-848`), передаётся в `g.max_speed` (`mtu3_gadget.c:709`),
  выбранная скорость ограничивается max (`mtu3_core.c:236-237`).
  Поэтому DT **ограничивает gadget High-Speed**, не SuperSpeed.
  Фактическая согласованная скорость остаётся hardware-вопросом.

**Ограничение переноса:** C не содержит `CONFIG_USB_MTU3*`, и vendor DT
содержит `usb-psy-name`, `typec-name`, `mediatek,uds` и другие свойства,
которые данный in-tree путь не доказывает. Также точный
`K/drivers/regulator/mtk-dvfsrc-regulator.c:142-153,168` матчится по
parent на mt8183/mt8192/mt6873, не доказывает MT6899 vendor regulator.
Не следует приравнивать точность provenance дерева к доказанному
соответствию его реализации поставляемому модулю.

## Q2. extcon-mtk-usb: charger и TCPC

[ИНДИКАТИВНО: MT6897@68a7bfe] Файл
`V/drivers/misc/mediatek/extcon/extcon-mtk-usb.c`:

- OF-match `mediatek,extcon-usb` (`:552-556`) соответствует OTA.
  `tcpc` — строка, lookup `tcpc_dev_get_by_name()` (`:363-389`),
  **не phandle**. При отсутствии имени/device helper возвращает
  `-ENODEV`, но probe только логирует и завершается 0 (`:526-535`):
  это не автоматический defer/retry для TCPC. Порядок TCPC→extcon важен.
- Получение `usb_role_switch_get()` может вернуть ошибку/defer,
  которая непосредственно возвращается (`:492-496`);
  role update вызывает `usb_role_switch_set_role()` (`:78-80`).
  TCP_NOTIFY sink переводит в DEVICE, но OTA включает
  `mediatek,bypss-typec-sink`; при таком флаге sink-ветка пропускается
  (`:325-332`, чтение `:508-510`). Следовательно power-supply detector
  (`:106-151`) нельзя списать как ненужную только по наличию TCPC.
- Это **прямые load-time импорты**, а не исключительно необязательные
  указатели: `usb_get_property` (`:116`) / `usb_set_property` (`:219`)
  экспортируются framework (`V/drivers/power/supply/mtk_charger.c:5438-5482`);
  `get_charger_by_name`, `charger_dev_cp_set_mode`,
  `charger_dev_enable_acdrv_manual` (`extcon-mtk-usb.c:212-225,251-252`)
  экспортируются `charger_class.c:995,1004,1182`. Внутри charger_class
  ops действительно вызываются по указателям (`:988-1003`), но это
  **не устраняет** ELF imports вызывающего extcon. Подтверждённые
  modinfo-depends из module-plan-analysis нельзя удалять.
- Если модуль framework отсутствует как поставщик символов, extcon
  не дойдёт до probe (unknown symbols). Если символы разрешены, но
  framework/charger device не пробился, psy helper пробует phandle,
  затем `primary_chg` и возвращает `-EINVAL` (`:169-197`); probe
  логирует и продолжает (`:521-535`). VBUS init тоже нефатален
  (`:503-506`), `cp_master` требуется для функционального VBUS-вызова
  (`:208-215`). Итого **degraded successful probe**, но не доказанный
  рабочий peripheral: psy-detector может не установиться, а TCPC sink
  bypass оставляет этот путь существенным.

## Q3. MT6375 и tcpci_late_sync

[ИНДИКАТИВНО: MT6897@68a7bfe]

- `V/drivers/mfd/mt6375.c:393-412` создаёт regmap, проверяет device ID,
  регистрирует IRQ chip и populates OF children; IRQ domain и parent
  I2C IRQ обрабатываются в `:303-325`. TCPC — platform child, а не
  самостоятельный I2C driver.
- `V/drivers/misc/mediatek/typec/tcpc/tcpc_mt6375.c:2702-2705` получает
  parent regmap (нет → `-ENODEV`, не defer), проверяет revision,
  разбирает DT и требует IIO channels (`:2708-2739`). Требование IIO —
  дополнительный provider gate, которого одно OF совпадение не закрывает.
  Имя descriptor по умолчанию `type_c_port0` (`:447`), DT может задать
  `tcpc,name` (`:2568`); `tcpc_device_register()` вызывается в `:2480` из
  probe `:2741`, затем reset/FOD и IRQ `pd_evt` (`:2458-2469,2747-2778`).
  Ошибка после регистрации удаляет TCPC. Видимость имени не доказывает
  успешную полную инициализацию.
- `tcpci_late_sync.c:44-79` обходит **все** class devices и вызывает
  `tcpc_device_irq_enable()`, без host/DP фильтра.
  `tcpci_core.c:450-475` делает `tcpci_init`, `tcpc_typec_init` и запускает
  event work; это нужно также для sink/peripheral attach, **не только
  host/DP**. В этом локальном Type-C дереве иных вызовов этой функции нет.
  Сохранить late_sync, загружать после успешной регистрации TCPC:
  class walk однократный, поздно появившийся child не покрыт сам по себе.
  PD work дополнительно может зависеть от charger при
  `CONFIG_USB_PD_WAIT_BC12` (`tcpci_core.c:549-555`); данный vendor CONFIG
  в C отсутствует, точное поведение MT6899 остаётся неизвестным.

## Q4. Нужны ли дополнительные драйверы peripheral ACM

Все строки таблицы: **[ИНДИКАТИВНО: MT6897@68a7bfe]**.
«Не нужен» означает отсутствие требования для **USB2 High-Speed configfs
ACM в разобранной реализации**, не разрешение вырезать реальные ELF
зависимости остальных сохранённых модулей или гарантию физической разводки.

| Модуль | Для HS peripheral ACM | Source evidence / ограничения |
| --- | --- | --- |
| usb_boost | Не нужен; не добавлять | `V/drivers/misc/mediatek/usb/usb_boost/usb_boost_plat.c:184-223` регистрирует CPU/core/DRAM/VCORE performance actions, QoS/regulator/audio boosting, не роль/UDC. Гипотеза «VBUS-only/host-only» **не подтверждена**: это performance driver, применимый и к traffic; не prerequisite ACM. |
| usb_meta | Не нужен; не добавлять | `V/drivers/misc/mediatek/usb/usb_meta/usb_meta.c:1314-1317,1346-1386,1399` включается boot META-параметрами, регистрирует собственный composite gadget/VCOM; может конкурировать за тот же UDC. Это не provider configfs ACM. |
| bootprof | Не добавлять ради META | Следует из ELF depends usb_meta→bootprof в `build/audit/module-plan-analysis.md`; отсутствие META-корня не требует включения logging dependency. |
| mtk-dvfsrc-helper | Не добавлять ради boost | ELF depends usb_boost→helper в том же анализе. Не путать helper с уже выбранными dvfsrc/regulator/power-domain providers. |
| usb_dp_selector | Не нужен для D+/D− HS; кандидат на удаление корня | `V/drivers/misc/mediatek/typec/mux/usb_dp_selector.c:62-121,161-210` выбирает USB TX1/TX2 и DP lanes/HPD, `:283-309` регистрирует switch/mux. Не USB2 role/UDC. OF-table `:325-336` матчится на `mediatek,usb_dp_selector` без MODULE_DEVICE_TABLE. |
| mux_switch | Не нужен непосредственно ACM; удалять только после пересчёта графа | `V/drivers/misc/mediatek/typec/mux/mux_switch.c:235-257,368-405` агрегирует orientation и mux callbacks. Не только DP, также USB orientation. OF-table `:424-434`: `mediatek,typec_mux_switch`, нет MODULE_DEVICE_TABLE; объясняет отсутствие alias, но не доказывает MT6899 binary binding. |
| ps5170 | Не нужен для USB2 HS; кандидат на удаление корня | `V/drivers/misc/mediatek/typec/mux/ps5170.c:63-71,221-226,310-367` — USB/DP lane redriver с ориентацией, **не только DP**. Probe регистрирует switch/mux и выключает их (`:525-569`); OF `:642` соответствует включённой OTA I2C-ноде. Код не доказывает board-level D+/D− трассу/отсутствие побочного power требования. |
| tcpci_late_sync | Сохранить | Общая Type-C init, включая sink (Q3), не DP-only. |
| extcon + charger dependencies | Сохранить | Default HOST (Q1), sink-bypass и power-supply DEVICE path (Q2); imports нельзя исключить по функциональной гипотезе. |

Итак две owner-unresolved строки выше получают **индикативное** source
объяснение binding, не статус exact owner/probe. Пересматривать старый
критерий «ноль безхозяинных USB-нод» следует с учётом ACM-необходимости,
а не автоматически загружать каждый присутствующий DP/debug driver.

## Q5. Четыре имени без provider: built-in НЕ подтверждён

[ИНДИКАТИВНО: MT6897@68a7bfe] Проверены Kconfig/Makefile и исходники
локальных деревьев, а не substring-поиск CONFIG по имени модуля.

| modinfo имя (дефис/подчёркивание нормализуются) | Реальный mapping в доступном source | C: статус и вердикт |
| --- | --- | --- |
| scpsys-dummy | `V/drivers/soc/mediatek/Makefile:15-16`: `CONFIG_DEVICE_MODULES_MTK_SCPSYS` → scpsys-dummy.o → mtk-scpsys-dummy.o; `Kconfig:39-46`, tristate. Экспорты notifier в `mtk-scpsys-dummy.c:72-86`. | Символ **отсутствует**, не =y/=m. Mapping найден только индикативно; built-in не подтверждён. |
| vcp_status_v2 | `V/drivers/misc/mediatek/vcp/rv/Makefile:1`: `CONFIG_MTK_TINYSYS_VCP_SUPPORT` → **vcp_status.o**, не vcp_status_v2.o; `vcp/Kconfig:1-15` (support tristate, version string). Экспорты rv/vcp_status.c:14-150 есть, но v2 mapping нет. | Support/version отсутствуют. Подмена v2 на vcp_status недопустима; exact CONFIG **не установлен**. |
| mtk-mmdebug-vcp-stub | `V/drivers/misc/mediatek/mmdebug/Makefile:18`: `CONFIG_MTK_MMDEBUG` → **mtk-mmdebug-vcp.o**; `Kconfig:3-10` tristate. Нет stub-файла/target или условного stub mapping при выключенном VCP в этой замене. | MMDEBUG отсутствует; exact stub CONFIG **не установлен**, built-in не подтверждён. |
| device-apc-mt6765 | `V/drivers/soc/mediatek/devapc/Makefile:7-57` строит common и SoC варианты mt6853…mt6989, но **не mt6765**. `V/drivers/misc/mediatek/apusys/Makefile:26,224-226` включает devapc/dapc.c в **apusys.o**, `CONFIG_MTK_APUSYS_SUPPORT` (`Kconfig:1`), не device-apc-mt6765.o. Это другой драйвер/target, не искомый provider. | APUSYS и DEVAPC vendor symbols отсутствуют; exact mt6765 CONFIG **не установлен**. Нельзя подтвердить mt6765 одним наличием dapc.c. |

Отсутствие проверено по всему C (`:1-7744`); положительный диагностический
факт: `C:337` — **`# CONFIG_ARCH_MEDIATEK is not set`**, при наличии
`CONFIG_IKCONFIG=y`/PROC (`C:163-164`). Конфиг может описывать GKI image,
в то время как external device modules собирались отдельным Kconfig и
имеют другую историю линковки. Следовательно отсутствие vendor CONFIG
не доказывает автоматически, что vendor-модуль не существовал; но
**не позволяет заявить**, что его exports встроены в реально используемый
kernel. Отдельный vendor build config не превращает external =y объект
в built-in GKI. Для закрытия нужны точный kernel exports/CRC и vendor
build provenance (например соответствующие vmlinux/Module.symvers и
проверка undefined symbols потребителей), а не догадка.

[НЕДОСТУПНО: пропавший репо] Точные CONFIG для v2/stub/mt6765 в ea65723
нельзя назвать честно: доступное родственное дерево их не содержит.
**Все четыре строки остаются unresolved для ворот G.** Если depmod видит
символы как kernel/built-in, он не создаёт .ko dependency edge; это
возможное объяснение отсутствия этих имён в поставляемом modules.dep,
**не доказательство** built-in. Возможны также несовпадающие/stale
modules.dep и build inputs. Сохранённый modinfo depends сильнее гипотезы
по отсутствующей строке dep: нужно сопоставить реальные ELF imports с
exports и modversions; forced loading запрещён.

## Q6. Рекомендация для ворот G (решение не внесено в SEEDS)

- **Высокая уверенность в fail-closed выводе:** G не закрывать, текущий
  90-план нельзя назвать полностью provider-validated. Четыре built-in
  гипотезы не подтверждены; provenance ea65723 отсутствует. Не добавлять
  фиктивные providers и не обходить integrity/modversions.
- **Средняя уверенность, индикативный source:** не добавлять четыре
  отсутствующих в плане U элемента usb_boost, helper, usb_meta, bootprof
  только ради равенства десяти USB-hinted корней: это performance/META,
  не доказанная дыра ACM. Их отсутствие в 90-плане обоснованно, но это не
  подтверждение всего плана.
- **Средняя уверенность:** предложить отдельное согласованное сокращение
  SEEDS usb_dp_selector/mux_switch/ps5170 для HS ACM после проверки
  точных ELF зависимостей и provider/board path. Пересчитать замыкание,
  не удалять вручную общий DRM/charger dependency tail: charger может
  сохранять значительную часть того же графа. Изменение этих трёх корней
  не гарантирует устранение четырёх G-blockers. До решения пользователя
  оставить 90-план неизменным; **автоматического approval удаления нет**.
- Сохранить TCPC→extcon/charger path и late_sync; проверить порядок и
  IIO/regulator/clock/genpd providers. Ни добавление boost/META, ни
  сокращение DP не решает само по себе HOST default и MT6899 ABI.

## Вердикт после source-evidence

| Доказано офлайн | Индикативно, не exact MT6899 binary evidence | Не закрыто этими входами / аппаратно недоказуемо |
| --- | --- | --- |
| OTA FDT структура, OTG + role-switch, отсутствие default-mode, HS; dtbo нет в payload (предыдущая проверка) | Vendor extcon OF/name lookup и degradation; imports подтверждены также инвентарём ELF | Реальный live DT, runtime providers/ordering, physical role event |
| [ТОЧНО: MiCode@2cd69ee] in-tree OTG default HOST, регистрация UDC и expected 11201000.usb0, HS clamp | MT6375 MFD/regmap/IRQ/IIO; late_sync также для sink | Соответствие поставляемого mtu3.ko in-tree реализации, ABI/CRC и фактический probe |
| Отсутствие vendor CONFIG в C; ARCH_MEDIATEK выключен | scpsys-dummy mapping; только non-v2 VCP/non-stub MMDEBUG; APUSYS dapc — не mt6765 target | Четыре missing exports: требуют дополнительных точных offline build/export артефактов; не считать их hardware-only вопросом |
| SEEDS/90-план не изменены; прямые USB imports не разрешается вырезать | Performance/META не обязательны ACM; DP/SS orientation не USB2 role; mux OF tables объясняют отсутствие aliases | Board-level routing, потребность конкретной ревизии PS5170, power/PHY; удаление требует решения |
| Кандидат host-built, unbooted, не рабочий порт | Source указывает необходимые переходы, не их успешность | UDC/bind/enumeration/ttyGS0/data/speed, boot и AVB acceptance, thermal/power безопасность |

Проверки этого прохода: `python3 -m unittest discover -s tests -v` —
89 PASS; `sh -n initramfs/init initramfs/usb-init` (и отдельно каждый
скрипт) — PASS; `git diff --check` — PASS. Эти host-проверки не валидируют
source-to-ELF ABI или аппаратный USB. Builder/ARM64 smoke не запускались:
изменён только этот документ. Основной репозиторий read-only; никаких
reboot, USB runtime изменений, прошивки, сети, commit или push не было.
