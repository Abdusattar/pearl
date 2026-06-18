# Складской модуль — Архитектура и решения

> Реализован: 18.06.2026

## Зачем Pearl ведёт склад (не 1С)

Повар и завхоз физически принимают товар и списывают продукты. Они не работают в 1С. Pearl даёт им простой интерфейс: приход → кнопка, списание → кнопка. Бухгалтер получает итоговый акт раз в месяц.

## Модели данных

### Product (расширен)
```python
unit      = String(10)   # кг, л, шт, г, уп, пач
category  = String(50)   # мясо, молочные, крупы, овощи, фрукты, прочее
```

### WarehouseReceipt (приходная накладная)
```
date, product_id, quantity, price_per_unit, total_cost
organization_id, supplier_name, transaction_id (→ OCR расход)
```

### WriteOff (акт списания / меню-требование)
```
date, product_id, quantity, organization_id
children_count  ← ключевое поле: сколько детей поели
reason          ← питание детей / порча / инвентаризация
```

## Расчёт остатков

```
balance = SUM(WarehouseReceipt.quantity) - SUM(WriteOff.quantity)
          WHERE organization_id IN (current_org + descendants)
          AND deleted_at IS NULL
```

Средняя взвешенная цена: `SUM(total_cost) / SUM(quantity)` из приходов.

## Юнит-экономика (цель)

`children_count` на каждом списании позволяет считать:
```
себестоимость питания на ребёнка в день =
  SUM(write_offs.quantity × avg_price) / SUM(write_offs.children_count)
```
Это KPI для собственников: сколько стоит накормить 1 ребёнка в каждой локации.

## Авто-создание прихода из квитанции (18.06 вечер)

При подтверждении квитанции с категорией `warehouse_eligible = true`:
- Показывается зелёный чекбокс «Добавить в склад» (по умолчанию включён)
- При Провести → `WarehouseReceipt` создаётся для каждой позиции с qty > 0 и price > 0
- `WarehouseReceipt.transaction_id` = id созданной транзакции расхода

**Важно:** чекбокс управляется флагом категории, не именем. «Готовая еда» чекбокс не показывает — она идёт прямо в расход (7510), не на склад.

## Цветовая индикация остатков

- Красный: `balance < 1` (критический минимум)
- Жёлтый: `1 ≤ balance < 3`
- Нормальный: `balance ≥ 3`

## Роуты

| URL | Описание |
|---|---|
| GET /warehouse/ | Остатки по категориям + последние движения |
| GET/POST /warehouse/receipt/add | Приход (с inline созданием нового продукта) |
| GET/POST /warehouse/writeoff/add | Списание (только продукты с остатком > 0) |
| GET /warehouse/products/ | Каталог + добавление |
| POST /warehouse/products/add | Создать продукт |
