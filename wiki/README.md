# Wiki — Жемчужина

> Скомпилированные знания проекта. Первый Ingest: 2026-06-14.
> Источник: context/, logs/terminal/, банки/

## Разделы

### architecture/
- [stack.md](architecture/stack.md) — технологический стек, принципы, клиентская матрица
- [modules.md](architecture/modules.md) — все модули ИС, статус реализации, roadmap
- [deploy_ops.md](architecture/deploy_ops.md) — Railway CLI, пайплайн деплоя, разбор инцидента «медленный прод» → auth-баг
- [menu_module.md](architecture/menu_module.md) — Меню/приёмы пищи, dish_id на списании (10.07)
- [unit_economics_module.md](architecture/unit_economics_module.md) — Сотрудники/ФОТ, Амортизация (живой расчёт, не Transaction), роль founder (10.07)

### payments/
- [strategy.md](payments/strategy.md) — стратегия оплат: ручной ввод → автоматика, два банка
- [optima.md](payments/optima.md) — Optima API, callback, PIN-модель, открытые вопросы

### ocr/
- [decision.md](ocr/decision.md) — история выбора OCR: EasyOCR → OpenRouter Vision, следующие шаги

### stakeholders/
- [roles.md](stakeholders/roles.md) — роли, доступ, масштаб, как принимаются решения

### blueprints/
> Проработка модулей ДО реализации — модель данных, экраны, открытые
> вопросы к заказчику. После реализации — переносится в architecture/.
- [menu_module.md](blueprints/menu_module.md) — Меню/приёмы пищи — **реализовано**, актуальная версия в architecture/
- [unit_economics_module.md](blueprints/unit_economics_module.md) — Сотрудники/ФОТ, Амортизация — **реализовано**, актуальная версия в architecture/
