"""Бот, шаг 2 (24.09): сообщение о деньгах или скрин банка → вопрос «Верно?» в личку
тому, чьи это деньги → «да» записывает той же функцией, что и форма в приложении.

Владелец 24.09: «автоматизировать так, чтобы мы тут с тобой не давали добро». Поэтому
подтверждает не владелец и не Махабат, а источник: чей карман — тот и отвечает за
снятие; кто отдал — за передачу; кто платил — за оплату поставщику; держатель
счёта — за остаток в банке. Одно слово в личке, без входа в приложение.

Бот не пишет, если не понял кто/откуда (не спрашиваем того, чьё имя угадали), если
такое уже записано (скажет в группе «уже есть»), если у человека нет лички с ботом.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import BotMessage, Organization, Reconciliation, User
from app.services import bot_group as grp
from app.services import cash
from app.services.purchases import OPERATIONAL_ROLES, site_orgs

OFFER = "money_offer"
OFFER_DAYS = 2          # «да» на вопрос позавчерашнего дня уже не ответ, а новый разговор

_DATE_FMT = "%Y-%m-%d"


def _account(db: Session, site: Organization, author: User | None, said: str | None) -> Organization | None:
    """Счёт: сказано «со школы» / «садика» — он; нет — по объекту автора."""
    orgs = site_orgs(db, site.id)
    if said in ("школа", "садик"):
        want = "school" if said == "школа" else "kindergarten"
        hit = [o for o in orgs if o.type == want]
        return hit[0] if len(hit) == 1 else None
    acc = grp._account_org(db, site, author)
    return acc if acc.id in {o.id for o in orgs} else None


def _label(org: Organization) -> str:
    return "школы" if org.type == "school" else "садика"


def _can_answer(u: User | None) -> bool:
    return u is not None and u.tg_id is not None and u.deleted_at is None and u.role in OPERATIONAL_ROLES + ("owner",)


def _kop(x, signed: bool = False) -> str:
    """Остаток счёта с копейками: «1,35», а не «1» (Мунара 24.09 ответила «нет» на «1»)."""
    x = Decimal(str(x))
    if x == x.to_integral_value():
        return grp._signed(x) if signed else grp.fmt_money(x)
    s = f"{abs(x):,.2f}".replace(",", " ").replace(".", ",")
    return ("+" if signed and x > 0 else "−" if x < 0 else "") + s


def build(db: Session, site: Organization, author: User | None, info: dict, today: date) -> dict | None:
    """Понятое сообщение → что записать и кого спросить. None — спрашивать нечего."""
    kind, amount = info.get("kind"), info.get("amount")
    d = info.get("date") or today
    if kind == grp.WITHDRAWAL and amount:
        pocket = grp.find_person(db, info.get("who")) or author
        acc = _account(db, site, author, info.get("account"))
        if pocket is None or acc is None or grp.match_withdrawal(db, site.id, amount, d)["status"] == "found":
            return None
        return {"op": "withdraw", "amount": str(amount), "date": d.strftime(_DATE_FMT), "account_org_id": acc.id,
                "pocket_user_id": pocket.id, "ask": pocket.id,
                "what": f"снятие {grp.fmt_money(amount)} со счёта {_label(acc)} в карман {grp._first(pocket.name)}"}
    if kind == grp.TRANSFER and amount:
        giver, taker = grp.find_person(db, info.get("who")), grp.find_person(db, info.get("to"))
        if giver is None and taker is not None and author is not None and author.id != taker.id:
            giver = author
        if taker is None and giver is not None and author is not None and author.id != giver.id:
            taker = author
        if giver is None or taker is None or giver.id == taker.id:
            return None
        # Учредителям карманов нет (24.09): деньги им — изъятие, от них — взнос; спрашиваем
        # того, чей карман. Оба учредителя — не наше движение.
        if giver.role == "founder" and taker.role == "founder":
            return None
        if taker.role == "founder":
            return {"op": "founder_out", "amount": str(amount), "date": d.strftime(_DATE_FMT), "founder_id": taker.id,
                    "pocket_user_id": giver.id, "ask": giver.id,
                    "what": f"передача {grp.fmt_money(amount)} учредителю {grp._first(taker.name)} из кармана {grp._first(giver.name)}"}
        if giver.role == "founder":
            return {"op": "founder_in", "amount": str(amount), "date": d.strftime(_DATE_FMT), "founder_id": giver.id,
                    "pocket_user_id": taker.id, "ask": taker.id,
                    "what": f"{grp.fmt_money(amount)} от учредителя {grp._first(giver.name)} в карман {grp._first(taker.name)}"}
        if grp.match_transfer(db, site.id, amount, d)["status"] == "found":
            return None
        return {"op": "transfer", "amount": str(amount), "date": d.strftime(_DATE_FMT), "from_user_id": giver.id,
                "to_user_id": taker.id, "ask": giver.id,
                "what": f"передача {grp._first(giver.name)} → {grp._first(taker.name)} {grp.fmt_money(amount)}"}
    if kind == grp.SUPPLIER_PAY and amount:
        supplier = grp.find_supplier(db, info.get("supplier"))
        payer = grp.find_person(db, info.get("who")) or author
        if supplier is None or payer is None \
                or grp.match_supplier_payment(db, site.id, amount, supplier, d)["status"] == "found":
            return None
        return {"op": "supplier", "amount": str(amount), "date": d.strftime(_DATE_FMT), "supplier_id": supplier.id,
                "payer_id": payer.id, "ask": payer.id,
                "what": f"оплата {supplier.name} {grp.fmt_money(amount)} из кармана {grp._first(payer.name)}"}
    if kind == "pocket" and author is not None and amount is not None:
        # своя точка наличных: разница с записями видна сразу, «да, причина» — причина
        expected = cash.pocket_balance(db, site.id, author.id)
        delta = Decimal(str(amount)) - expected
        tail = " — с записями сходится" if abs(delta) < 1 else \
            f". По записям {grp.fmt_money(expected)}, разница {grp._signed(delta)}"
        return {"op": "recount", "amount": str(amount), "date": today.strftime(_DATE_FMT), "pocket_user_id": author.id,
                "ask": author.id, "what": f"наличных у {grp._first(author.name)} на руках {grp.fmt_money(amount)}{tail}",
                "matches": abs(delta) < 1}
    if kind in (grp.BALANCE, grp.BANK):
        bal = info.get("balance") or (info.get("amount") if kind == grp.BALANCE else None)
        if not bal or (kind == grp.BANK and info.get("bank_op") not in ("balance", None)):
            return None
        acc = _account(db, site, author, info.get("account"))
        if acc is None:
            return None
        from app.services.bot import _account_holder
        holder = _account_holder(db, acc)
        # Скрин сегодня утром = конец вчерашнего дня: Optima за вчера зачислена ночью,
        # за сегодня ещё нет (память: зачисление на следующий день)
        def recorded(day):
            return db.query(Reconciliation.id).filter(Reconciliation.organization_id == acc.id,
                                                      Reconciliation.kind == "account", Reconciliation.date == day,
                                                      Reconciliation.cancelled_at.is_(None)).first() is not None
        on = today - timedelta(days=1) if d >= today else d
        if d >= today and recorded(on):
            # вчера уже записано — второй скрин за день показывает счёт после сегодняшних
            # движений (Мунара 24.09: скрин после снятия 64 662, разница −134 — комиссия)
            on = today
        if recorded(on):
            return None   # остаток на этот день уже записан — второй скрин не переспрашиваем
        first = not db.query(Reconciliation.id).filter(Reconciliation.organization_id == acc.id,
                                                       Reconciliation.kind == "account",
                                                       Reconciliation.cancelled_at.is_(None)).first()
        fee, delta = None, Decimal(0)
        if first:
            # первая цифра по счёту (школа 24.09): сравнивать не с чем — «по записям 80» сбило Айжан
            tail = " — первая цифра по этому счёту, станет точкой отсчёта"
        else:
            expected = cash.expected_account(db, acc.id, on)
            delta = Decimal(str(bal)) - expected
            tail = " — с записями сходится" if abs(delta) <= 1 else \
                f". По записям {_kop(expected)}, разница {_kop(delta, signed=True)}"
            if -500 < delta < -1 and on == today and _withdrew_today(db, acc.id, today):
                # Комиссия за снятие (владелец 24.09: «если комиссия — пиши комиссия»): расход со
                # счёта, остаток сходится в ноль, никого не спрашиваем
                fee = -delta
                tail = f", комиссия банка {_kop(fee)}"
            if delta > max(Decimal("5000"), abs(expected) * Decimal("0.2")):
                # счёт не растёт сам: без прихода такой скачок — скорее не про счёт (наличные?)
                return {"op": "bank", "ask": holder.id if holder else None, "amount": str(bal),
                        "date": on.strftime(_DATE_FMT), "org_id": acc.id,
                        "what": f"остаток счёта {_label(acc)} {_kop(bal)}{tail}",
                        "doubt": "счёт не мог вырасти без прихода — возможно, это наличные"}
        o = {"op": "bank", "amount": str(bal), "date": on.strftime(_DATE_FMT), "org_id": acc.id,
             "ask": holder.id if holder else None, "matches": not first and (abs(delta) <= 1 or fee is not None),
             "what": f"остаток счёта {_label(acc)} {_kop(bal)} на конец {grp._dd(on)}{tail}"}
        if fee is not None:
            o["fee"] = str(fee)
        return o
    return None


def offer(db: Session, site: Organization, author: User | None, info: dict, today: date) -> BotMessage | None:
    """Спросить источник в личке. Возвращает отправленный вопрос (user_id — кого спросили)."""
    from app.services.bot import send
    o = build(db, site, author, info, today)
    if o is None or not o.get("ask"):
        return None
    who = db.get(User, o["ask"])
    if not _can_answer(who):
        return None
    if o.get("doubt"):
        # Неправдоподобно (владелец 24.09: «бот гонит») — человека не дёргаем, владельцу тихо
        from app.services.bot import owner_copy
        owner_copy(db, f"{who.name}: {o['what']} — не спросил, {o['doubt']}. Разберитесь в Кассе.")
        return None
    if o["op"] == "recount" and not o.get("matches") and _drafts_pending(db, site):
        # Чеки ещё не проведены (24.09: «по записям 69 891» при непроведённых 26 340) — сверять
        # рано. Цифру держим, сверим, когда Махабат проведёт (settle_deferred).
        prev = open_offer(db, who)
        if prev is not None:
            prev.status = "superseded"
        for old in db.query(BotMessage).filter(BotMessage.kind == OFFER, BotMessage.user_id == who.id,
                                               BotMessage.status == "deferred").all():
            old.status = "superseded"
        m = send(db, who.tg_id, _deferred_text(db, site, who, Decimal(o["amount"])), OFFER, user_id=who.id, payload=o)
        m.status = "deferred"
        return m
    d = datetime.strptime(o["date"], _DATE_FMT).date()
    day = "сегодня" if d == today else ("вчера" if d == today - timedelta(days=1) else grp._d(d))
    when = "" if o["op"] in ("bank", "recount") else f", {day}"
    # Один открытый вопрос на человека: новый заменяет старый без «поправок»
    prev = open_offer(db, who)
    if prev is not None:
        prev.status = "superseded"
    if o.get("matches") or trusted(db, who, o["op"]):
        # Ступень 3: пять «да» подряд без «нет» — пишем сами, человек видит «записал».
        # Сходится с записями (владелец 24.09: «если всё совпадает, переспрашивать не нужно») — тоже.
        m = send(db, who.tg_id, "", OFFER, user_id=who.id, payload=o, status="logged")
        res = answer(db, site, who, m, True, None, auto=True)
        m.text = f"Записал: {o['what']}{when}." + ("" if o.get("matches") else " Если не так — напишите «не так».")
        m.status = "answered"
        from app.services.bot import send as _send
        _send(db, who.tg_id, m.text, "money_auto", user_id=who.id, payload={**o, "result": res})
        return m
    text = f"{grp._first(who.name)}, {o['what']}{when}. Верно? Да / нет"
    return send(db, who.tg_id, text, OFFER, user_id=who.id, payload=o)


def trusted(db: Session, who: User, op: str) -> bool:
    """Ступень 3 (24.09): последние N вопросов этого вида человеку — все «да», ни одного «нет»."""
    from app.services import rules
    n = int(rules.get(db, "auto_after_yes") or 0)
    if n <= 0:
        return False
    last = (db.query(BotMessage).filter(BotMessage.kind == OFFER, BotMessage.user_id == who.id,
                                        BotMessage.payload["op"].astext == op,
                                        BotMessage.status.in_(("answered", "declined")))
            .order_by(BotMessage.id.desc()).limit(n).all())
    return len(last) >= n and all(m.status == "answered" and not (m.payload or {}).get("auto") for m in last)


def asked_note(db: Session, msg: BotMessage | None) -> str:
    """Хвост к ответу в группе / владельцу: кого бот спросил."""
    if msg is None:
        return ""
    who = db.get(User, msg.user_id)
    return f" Спросил {grp._first(who.name)} в личке, запишу после «да»." if who else ""


def open_offer(db: Session, user: User) -> BotMessage | None:
    since = datetime.combine(date.today() - timedelta(days=OFFER_DAYS - 1), datetime.min.time())
    return (db.query(BotMessage).filter(BotMessage.kind == OFFER, BotMessage.user_id == user.id,
                                        BotMessage.status.in_(("sent", "logged")), BotMessage.created_at >= since)
            .order_by(BotMessage.id.desc()).first())


def answer(db: Session, site: Organization, user: User, ask: BotMessage, yes: bool, reason: str | None,
           auto: bool = False) -> str:
    """«да» — записать тем, кто подтвердил; «нет» — не записывать, сказать, где поправить."""
    from app.services import ledger
    from app.services.bot import public_url
    o = ask.payload or {}
    if not yes:
        ask.status = "declined"
        return f"Не записал. Если было по-другому — внесите в приложении: {public_url()}/new/cash"
    amount, d = Decimal(o["amount"]), datetime.strptime(o["date"], _DATE_FMT).date()
    note = "из бота, подтвердил(а) в личке"
    try:
        if o["op"] == "withdraw":
            rec = cash.withdraw(db, user=user, site_org_id=site.id, account_org_id=o["account_org_id"], amount=amount,
                                d=d, comment=note, pocket_user_id=o["pocket_user_id"])
        elif o["op"] == "transfer":
            rec = cash.transfer(db, user=user, site_org_id=site.id, from_user_id=o["from_user_id"],
                                to_user_id=o["to_user_id"], amount=amount, d=d, comment=note)
        elif o["op"] == "supplier":
            rec = ledger.pay_supplier(db, user=user, site_org_id=site.id, supplier_id=o["supplier_id"], amount=amount,
                                      d=d, source="cash", payer_id=o["payer_id"], account_org_id=None, comment=note)
        elif o["op"] == "founder_out":
            rec = cash.founder_withdraw(db, user=user, site_org_id=site.id, founder_id=o["founder_id"],
                                        pocket_user_id=o["pocket_user_id"], amount=amount, d=d, comment=note)
        elif o["op"] == "founder_in":
            rec = cash.founder_fund(db, user=user, site_org_id=site.id, founder_id=o["founder_id"],
                                    pocket_user_id=o["pocket_user_id"], amount=amount, d=d, comment=note)
        elif o["op"] == "recount":
            rec = cash.recount(db, user=user, site_org_id=site.id, pocket_user_id=o["pocket_user_id"], actual=amount, d=d,
                               reason=reason or "по сообщению в боте")
        else:
            if o.get("fee"):
                cash.bank_fee(db, user=user, org_id=o["org_id"], amount=Decimal(o["fee"]), d=d)
            # разница видна в Кассе как есть; «да, причина» — причина пишется рядом
            rec = cash.bank_balance(db, user=user, org_id=o["org_id"], actual=amount, d=d,
                                    reason=reason or ("комиссия банка" if o.get("fee") else "по скрину, подтверждено в боте"))
    except ValueError as e:
        return f"Не записал: {e}. Поправьте в приложении: {public_url()}/new/cash"
    ask.status = "answered"
    ask.payload = {**o, "result_id": rec.id, "auto": auto}
    return "Записал. Спасибо!"


def _withdrew_today(db: Session, account_org_id: int, today: date) -> bool:
    from app.models import CashFunding
    return db.query(CashFunding.id).filter(CashFunding.source_type == "withdrawal", CashFunding.date == today,
                                           CashFunding.deleted_at.is_(None),
                                           (CashFunding.account_org_id == account_org_id)
                                           | ((CashFunding.account_org_id.is_(None))
                                              & (CashFunding.organization_id == account_org_id))).first() is not None


def _drafts_pending(db: Session, site: Organization) -> bool:
    from app.services import today as td
    return bool(td.unchecked_receipts(db, site.id))


def settle_deferred(db: Session, site: Organization, today: date) -> list[str]:
    """Чеки проведены — сверяем отложенные цифры наличных: сошлось — записали молча,
    нет — один вопрос человеку."""
    if _drafts_pending(db, site):
        return []
    done = []
    for m in db.query(BotMessage).filter(BotMessage.kind == OFFER, BotMessage.status == "deferred").all():
        who = db.get(User, m.user_id)
        m.status = "superseded"
        if who is None:
            continue
        info = {"kind": "pocket", "amount": float(m.payload["amount"]), "date": today}
        res = offer(db, site, who, info, today)
        done.append(f"deferred:{m.id}:{res.id if res is not None else 'none'}")
    return done


def _deferred_text(db: Session, site: Organization, who: User, stated: Decimal) -> str:
    """Владелец 24.09: «сказать, что у вас спишется и станет столько, когда Махабат подтвердит» —
    человек видит, что закупы подготовлены, и сам подталкивает учётчика."""
    from app.services import today as td
    have = cash.pocket_balance(db, site.id, who.id)
    mine = [r for r in td.unchecked_receipts(db, site.id) if r.created_by == who.id]
    prepared = sum(Decimal(str((r.payload or {}).get("amount") or 0)) for r in mine)
    after = have - prepared
    head = f"Принял {grp.fmt_money(stated)}. По записям у вас {grp.fmt_money(have)}"
    if not prepared:
        return head + ", чеки ещё не проведены — сверю после проверки Махабат."
    tail = " — как у вас." if abs(after - stated) < 1 else f"; сверю с вашими {grp.fmt_money(stated)} после подтверждения."
    return (head + f", закупы на {grp.fmt_money(prepared)} подготовлены: спишутся, когда Махабат подтвердит их "
            f"в системе, и станет {grp.fmt_money(after)}" + tail)
