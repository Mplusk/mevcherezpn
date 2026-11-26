# Порядок применения серверной защиты

Это операторская инструкция для согласованного развёртывания. `node_operator.py`
и транспорт ещё не прошли настоящий end-to-end deploy: до применения проверить
их на изолированном стенде и зафиксировать digest исходников. Main preflight
проверялся на реальном Linux без изменений через API; это не проверка stage.
Ключ и содержимое конфигураций не выводить и не передавать в лог.

Ниже команды запускаются с доверенного Windows-компьютера из корня проверенного
checkout; OpenSSH должен иметь заранее независимо проверенные host keys.
На каждом узле обязателен работоспособный RO mount, базовые VPN и мониторинг.

```powershell
$routePython = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$routeTx = [guid]::NewGuid().ToString('N')
& $routePython deploy/ru_guard/node_controller.py preflight main $routeTx
& $routePython deploy/ru_guard/node_controller.py preflight de $routeTx
& $routePython deploy/ru_guard/node_controller.py preflight pl $routeTx
& $routePython deploy/ru_guard/node_controller.py preflight nl $routeTx
```

Продолжать только при `NODE_OK` на **всех** четырёх. Сохранить `$routeTx` в
операторском журнале: он идентифицирует отдельные локальные backups на узлах.
При `NODE_NOT_CONFIRMED_RECONCILE` или `NODE_OUTCOME_UNKNOWN_RECONCILE` не
повторять действия автоматически. Сверить состояние panel apply, readback
шаблонов и root-only журнал `guard-transactions/<TX>/operator.json` и backup.

После успешных проверок: по одному узлу, дожидаясь `NODE_OK` и независимой
проверки VPN-соединения перед переходом к следующему:

```powershell
& $routePython deploy/ru_guard/node_controller.py stage de $routeTx
& $routePython deploy/ru_guard/node_controller.py verify-staged de $routeTx
# Проверить настоящий VPN-трафик через DE из независимой точки мониторинга.
& $routePython deploy/ru_guard/node_controller.py stage pl $routeTx
& $routePython deploy/ru_guard/node_controller.py verify-staged pl $routeTx
# Аналогично проверить PL, затем выполнять NL и main.
& $routePython deploy/ru_guard/node_controller.py stage nl $routeTx
& $routePython deploy/ru_guard/node_controller.py verify-staged nl $routeTx
& $routePython deploy/ru_guard/node_controller.py stage main $routeTx
& $routePython deploy/ru_guard/node_controller.py verify-staged main $routeTx
```

`NODE_OK` подтверждает конфигурацию и статус служб конкретного узла, но не
заменяет независимую проверку VPN, подписки и доступности сайта. До записи
родительского outbound повторно убедиться, что на каждом узле `verify-staged`
и внешняя проверка успешны. Контроллер ниже ещё раз проверяет все четыре узла,
сохраняет root-only backup и пишет журнал на основном узле, затем активирует
правило и проверяет readback и сгенерированные конфигурации на всех четырёх:

```powershell
& $routePython deploy/ru_guard/outbound_controller.py activate $routeTx
```

Sync может происходить асинхронно. До `ALL_NODE_CONFIGURATIONS_OK` и отдельной
успешной проверки VPN в независимой сети считать раскатку незавершённой.
Проверки четырёх узлов последовательные и не являются распределённой блокировкой;
не выполнять другие правки панели и CI-операции во время этой процедуры.

Откат после активации начинают с отключения исключения через контроллер:

```powershell
& $routePython deploy/ru_guard/outbound_controller.py rollback $routeTx
```

Он записывает отдельный backup, отключает исключение и проверяет staged на
четырёх узлах. При неопределённом apply сначала сверить состояние вручную,
не запускать rollback вслепую. Только после `ALL_NODE_CONFIGURATIONS_OK`
возможно восстановление шаблонов, по одному узлу:

```powershell
& $routePython deploy/ru_guard/node_controller.py rollback de $routeTx
```

Повторить `rollback` для тех узлов, где действительно установлен stage, после
каждого проверить прежний VPN внешней точкой. При занятом/неизвестном apply,
активном исключении, отсутствующем mount или backup код прекращает работу;
не отсоединять mount, пока восстановление и отсутствие зависимости от него
не доказаны. Ошибка после PATCH или старта apply означает неопределённое
состояние, а не разрешение выполнить действие повторно без сверки.
