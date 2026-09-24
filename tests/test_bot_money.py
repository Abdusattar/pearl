"""Бот, шаг 2 (24.09): сообщение о деньгах / скрин банка → «Верно?» в личку источнику →
«да» записывает, «нет» — нет. Модель подменена."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import BotMessage, CashFunding, CashTransfer, Organization, Reconciliation, Supplier, User
from app.services import bot as svc
from app.services import bot_group as grp
from app.services import bot_money

GROUP = -1001234567891


@pytest.fixture()
def world(db, monkeypatch, tmp_path):
    root = Organization(name="Корень тест-дн", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-дн", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    m = User(name="Махабаттест Дн", role="staff", organization_id=sadik.id, tg_id=900000000201)
    n = User(name="Мунаратест Дн", role="manager", organization_id=sadik.id, tg_id=900000000202)
    db.add_all([m, n])
    db.flush()
    monkeypatch.setattr(svc, "site_for_bot", lambda db: sadik)
    monkeypatch.delenv(svc.TOKEN_ENV, raising=False)
    monkeypatch.setenv(svc.GROUP_ENV, str(GROUP))
    monkeypatch.setattr(svc, "download_file", lambda file_id: b"photo-" + file_id.encode())
    monkeypatch.setattr(svc, "_account_holder", lambda db, org: n)
    from app.services import drafts
    monkeypatch.setattr(drafts, "MEDIA_ROOT", tmp_path)
    return {"sadik": sadik, "m": m, "n": n}


def _model(monkeypatch, answer: dict):
    monkeypatch.setattr(grp, "ask_model", lambda prompt, image=None, mime="image/jpeg", pdf=None: answer)


def _group(user, text=None, photo_id=None, mid=1):
    msg = {"message_id": mid, "chat": {"id": GROUP, "type": "supergroup"}, "from": {"id": user.tg_id}}
    if text is not None:
        msg["text" if photo_id is None else "caption"] = text
    if photo_id:
        msg["photo"] = [{"file_id": photo_id, "file_unique_id": "u-" + photo_id, "file_size": 10}]
    return {"message": msg}


def _private(user, text, mid=50):
    return {"message": {"message_id": mid, "chat": {"id": user.tg_id, "type": "private"},
                        "from": {"id": user.tg_id}, "text": text}}


def _offer(db, user):
    return db.query(BotMessage).filter_by(kind=bot_money.OFFER, user_id=user.id).order_by(BotMessage.id.desc()).first()


def test_withdrawal_in_group_asks_pocket_owner_and_yes_records(db, world, monkeypatch):
    n = world["n"]
    _model(monkeypatch, {"kind": "withdrawal", "amount": 25000, "who": None, "date": "today", "sure": True})
    svc.handle_update(db, _group(n, "сняла 25 000"))
    o = _offer(db, n)
    assert o is not None and "снятие 25 000 со счёта садика в карман Мунаратест" in o.text
    assert db.query(CashFunding).filter_by(organization_id=world["sadik"].id).count() == 0   # до «да» — ничего
    reply = svc.handle_update(db, _private(n, "да"))
    assert reply == "Записал. Спасибо!"
    f = db.query(CashFunding).filter_by(organization_id=world["sadik"].id).one()
    assert f.amount == Decimal("25000") and f.accountable_user_id == n.id and f.created_by == n.id
    assert _offer(db, n).status == "answered"


def test_no_declines_and_nothing_written(db, world, monkeypatch):
    m, n = world["m"], world["n"]
    _model(monkeypatch, {"kind": "transfer", "amount": 20000, "who": None, "to": "Махабаттест", "date": "today"})
    svc.handle_update(db, _group(n, "передала Махабат 20 000"))
    o = _offer(db, n)
    assert "передача Мунаратест → Махабаттест 20 000" in o.text
    assert "Не записал" in svc.handle_update(db, _private(n, "нет"))
    assert db.query(CashTransfer).filter_by(site_org_id=world["sadik"].id).count() == 0


def test_transfer_said_by_receiver_asks_giver(db, world, monkeypatch):
    m, n = world["m"], world["n"]
    _model(monkeypatch, {"kind": "transfer", "amount": 20000, "who": "Мунаратест", "to": None, "date": "today"})
    svc.handle_update(db, _group(m, "получила от Мунары 20 000"))
    assert _offer(db, n) is not None and _offer(db, m) is None
    svc.handle_update(db, _private(n, "да"))
    t = db.query(CashTransfer).filter_by(site_org_id=world["sadik"].id).one()
    assert (t.from_user_id, t.to_user_id) == (n.id, m.id)


def test_already_recorded_is_not_asked(db, world, monkeypatch):
    n = world["n"]
    from app.services import cash
    cash.withdraw(db, user=n, site_org_id=world["sadik"].id, account_org_id=world["sadik"].id,
                  amount=Decimal("25000"), d=date.today())
    _model(monkeypatch, {"kind": "withdrawal", "amount": 25000, "date": "today"})
    svc.handle_update(db, _group(n, "сняла 25 000"))
    assert _offer(db, n) is None


def test_bank_screenshot_asks_holder_for_end_of_yesterday(db, world, monkeypatch):
    n = world["n"]
    _model(monkeypatch, {"kind": "bank", "bank_op": "balance", "balance": 64797.68, "date": date.today().isoformat(),
                         "sure": True})
    svc.handle_update(db, _group(n, photo_id="bank1"))
    o = _offer(db, n)
    y = date.today() - timedelta(days=1)
    assert o is not None and f"на конец {y.strftime('%d.%m')}" in o.text
    assert svc.handle_update(db, _private(n, "да, Optima 18.09 в пути")) == "Записал. Спасибо!"
    rec = db.query(Reconciliation).filter_by(kind="account", organization_id=world["sadik"].id).one()
    assert rec.date == y and rec.actual_amount == Decimal("64797.68") and rec.reason == "Optima 18.09 в пути"


def test_private_text_asks_self_once(db, world, monkeypatch):
    n = world["n"]
    _model(monkeypatch, {"kind": "withdrawal", "amount": 10000, "date": "yesterday"})
    reply = svc.handle_update(db, _private(n, "вчера сняла 10 000"))
    assert reply.startswith("Мунаратест, снятие 10 000") and ", вчера." in reply
    assert db.query(BotMessage).filter_by(kind=bot_money.OFFER, user_id=n.id).count() == 1
    svc.handle_update(db, _private(n, "да", mid=51))
    assert db.query(CashFunding).filter_by(organization_id=world["sadik"].id).one().date == date.today() - timedelta(days=1)


def test_unknown_person_is_not_guessed(db, world, monkeypatch):
    m = world["m"]
    _model(monkeypatch, {"kind": "transfer", "amount": 5000, "who": "Гульнара", "to": "Бакыт", "date": "today"})
    svc.handle_update(db, _group(m, "Гульнара передала Бакыту 5 000"))
    assert db.query(BotMessage).filter_by(kind=bot_money.OFFER).count() == 0


def test_on_hand_in_private_asks_self_and_yes_records_pocket_point(db, world, monkeypatch):
    """Айжан 24.09: «наличных школы у меня 12 400» в личку → «верно?» → «да, причина» → точка кармана."""
    n = world["n"]
    reply = svc.handle_update(db, _private(n, "наличных у меня на руках 12 400"))
    assert reply.startswith("Мунаратест, наличных у Мунаратест на руках 12 400")
    assert "Записал" in svc.handle_update(db, _private(n, "да, остаток с прошлого месяца", mid=51))
    rec = db.query(Reconciliation).filter_by(kind="pocket", subject_id=n.id).one()
    assert rec.actual_amount == Decimal("12400") and rec.reason == "остаток с прошлого месяца"


def test_excel_in_private_is_saved_and_owner_told(db, world, monkeypatch, tmp_path):
    n = world["n"]
    monkeypatch.setattr(svc, "MEDIA_ROOT", tmp_path)
    upd = {"message": {"message_id": 7, "chat": {"id": n.tg_id, "type": "private"}, "from": {"id": n.tg_id},
                       "document": {"file_id": "x1", "file_unique_id": "ux1", "file_name": "дети.xlsx",
                                    "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}}}
    reply = svc.handle_update(db, upd)
    assert reply == "Файл получил, передал Абдусаттару. Спасибо!"
    saved = db.query(BotMessage).filter_by(kind="file", user_id=n.id).one()
    assert (tmp_path / saved.payload["file"]).read_bytes() == b"photo-x1"
    assert db.query(BotMessage).filter_by(kind="owner_copy").count() == 1


def test_first_bank_figure_is_point_zero_and_question_goes_to_owner(db, world, monkeypatch):
    n = world["n"]
    _model(monkeypatch, {"kind": "bank", "bank_op": "balance", "balance": 2089650, "date": date.today().isoformat(), "sure": True})
    svc.handle_update(db, _group(n, photo_id="bank9"))
    o = _offer(db, n)
    assert "первая цифра" in o.text and "По записям" not in o.text
    reply = svc.handle_update(db, _private(n, "По каким записям?"))
    assert reply.startswith("Передал")
    assert db.query(BotMessage).filter_by(kind="owner_copy").count() == 1
    assert _offer(db, n).status in ("sent", "logged")   # вопрос открыт, «да» ещё сработает
    assert "Записал" in svc.handle_update(db, _private(n, "да", mid=52))


def test_bot_chat_page_is_owner_only_and_shows_dialog(client, db, world, monkeypatch):
    from app.models import User as _U
    owner = _U(name="Владелец тест-дн", role="owner", organization_id=world["sadik"].id, tg_id=900000000299,
               password_hash="x")
    db.add(owner)
    db.flush()
    n = world["n"]
    _model(monkeypatch, {"kind": "withdrawal", "amount": 25000, "date": "today"})
    svc.handle_update(db, _group(n, "сняла 25 000"))
    svc.handle_update(db, _private(n, "да"))
    db.commit()
    from app.dependencies import get_current_user
    monkeypatch.setattr("app.routers.new_bot.get_current_user", lambda request, db: n)
    assert client.get("/new/settings/bot/chat").status_code == 403
    monkeypatch.setattr("app.routers.new_bot.get_current_user", lambda request, db: owner)
    page = client.get("/new/settings/bot/chat").text
    assert "сняла 25 000" in page and "снятие 25 000" in page and "Записал. Спасибо!" in page
    page = client.get(f"/new/settings/bot/chat?who={n.id}").text
    assert "Записал. Спасибо!" in page


def test_parse_amount_thousands_vs_fraction():
    from app.services.bot import parse_amount
    assert parse_amount("90,055") == Decimal("90055")
    assert parse_amount("90 055") == Decimal("90055")
    assert parse_amount("12.500") == Decimal("12500")
    assert parse_amount("1,5") == Decimal("1.5")
    assert parse_amount("12.50") == Decimal("12.50")
    assert parse_amount("2 089 650,45") == Decimal("2089650.45")


def test_on_hand_with_thousands_comma(db, world):
    n = world["n"]
    reply = svc.handle_update(db, _private(n, "На руках-90,055"))
    assert "на руках 90 055" in reply


def test_bank_screenshot_in_group_is_never_echoed_in_group(db, world, monkeypatch):
    n = world["n"]
    monkeypatch.setenv(svc.GROUP_TALK_ENV, "1")
    _model(monkeypatch, {"kind": "bank", "bank_op": "balance", "balance": 64797.68, "date": date.today().isoformat(), "sure": True})
    svc.handle_update(db, _group(n, photo_id="bank7"))
    assert db.query(BotMessage).filter_by(kind="group_reply", chat_id=GROUP).count() == 0
    assert _offer(db, n) is not None and _offer(db, n).chat_id == n.tg_id


def test_transfer_to_founder_is_withdrawal_from_pocket(db, world, monkeypatch):
    from app.models import User as _U
    n = world["n"]
    f = _U(name="Айдайтест Дн", role="founder", organization_id=world["sadik"].id, tg_id=900000000203)
    db.add(f)
    db.flush()
    _model(monkeypatch, {"kind": "transfer", "amount": 50000, "who": None, "to": "Айдайтест", "date": "today"})
    svc.handle_update(db, _group(n, "передала Айдай 50 000"))
    o = _offer(db, n)
    assert o is not None and "учредителю Айдайтест из кармана Мунаратест" in o.text
    assert _offer(db, f) is None
    assert "Записал" in svc.handle_update(db, _private(n, "да"))
    assert db.query(CashTransfer).count() == 0
    from app.services import cash
    assert cash.pocket_balance(db, world["sadik"].id, n.id) == Decimal("-50000")


def test_money_from_founder_is_funding_into_pocket(db, world, monkeypatch):
    from app.models import User as _U
    n = world["n"]
    f = _U(name="Таластест Дн", role="founder", organization_id=world["sadik"].id, tg_id=900000000204)
    db.add(f)
    db.flush()
    _model(monkeypatch, {"kind": "transfer", "amount": 100000, "who": "Таластест", "to": None, "date": "today"})
    svc.handle_update(db, _group(n, "получила от Таласа 100 000"))
    o = _offer(db, n)
    assert "от учредителя Таластест в карман Мунаратест" in o.text
    svc.handle_update(db, _private(n, "да"))
    from app.services import cash
    assert cash.pocket_balance(db, world["sadik"].id, n.id) == Decimal("100000")


def test_second_bank_screenshot_same_day_records_today(db, world, monkeypatch):
    """Мунара 24.09: утром скрин → конец вчера; днём после снятия ещё скрин → сегодня, разница = комиссия."""
    from app.services import cash
    n = world["n"]
    cash.bank_balance(db, user=n, org_id=world["sadik"].id, actual=Decimal("64797.68"),
                      d=date.today() - timedelta(days=1), reason="утро")
    cash.withdraw(db, user=n, site_org_id=world["sadik"].id, account_org_id=world["sadik"].id,
                  amount=Decimal("64662"), d=date.today())
    _model(monkeypatch, {"kind": "bank", "bank_op": "balance", "balance": 1.35, "date": None, "sure": True})
    svc.handle_update(db, _group(n, photo_id="bank2"))
    o = _offer(db, n)
    # владелец 24.09: «если комиссия — пиши комиссия»: расход со счёта, без вопроса
    assert o.status == "answered" and o.text.startswith("Записал: остаток счёта садика 1,35 на конец "
                                                        + date.today().strftime("%d.%m") + ", комиссия банка 134,33.")
    rec = (db.query(Reconciliation).filter_by(kind="account", organization_id=world["sadik"].id)
           .order_by(Reconciliation.id.desc()).first())
    assert rec.date == date.today() and rec.reason == "комиссия банка" and rec.delta == 0
    from app.models import Transaction
    from app.models import ExpenseCategory
    fee = db.query(Transaction).filter_by(organization_id=world["sadik"].id, paid_directly=True).one()
    assert fee.amount == Decimal("134.33") and db.get(ExpenseCategory, fee.category_id).name == "Комиссия банка"


def test_cash_on_hand_in_group_is_pocket_not_bank(db, world, monkeypatch):
    """Мунара 24.09: «Остаток наличными 51090» в группе — наличные на руках, не счёт."""
    n = world["n"]
    _model(monkeypatch, {"kind": "balance", "amount": 51090, "date": "today", "sure": True})   # модель путает — не зовём
    svc.handle_update(db, _group(n, "Остаток наличными 51090"))
    o = _offer(db, n)
    assert o is not None and o.payload["op"] == "recount" and "на руках 51 090" in o.text
    assert db.query(BotMessage).filter_by(kind="group_reply").count() == 0


def test_purchase_amount_in_group_asks_for_receipt(db, world, monkeypatch):
    n = world["n"]
    reply = svc.handle_update(db, _group(n, "Закуп 13570"))
    assert reply.startswith("Мунаратест, закуп 13 570") and "фото чека" in reply and "проверит" in reply
    assert _offer(db, n) is None


def test_on_hand_phrases():
    from app.services.bot import on_hand_amount
    assert on_hand_amount("Нал остаток 51090-12700=38 390") == Decimal("38390")
    assert on_hand_amount("на руках 15 000") == Decimal("15000")
    assert on_hand_amount("Остаток наличными 51090") == Decimal("51090")
    assert on_hand_amount("нал. 0") == Decimal("0")
    assert on_hand_amount("сняла 25 000") is None
    assert on_hand_amount("Снятия с банка 64662") is None


def test_commission_word_is_yes_with_reason_and_kopecks_shown(db, world, monkeypatch):
    """Мунара 24.09: на «ответьте „да, комиссия“» написала «Комиссия» — бот не понял, потом «Нет»,
    остаток 1,35 потерян. Теперь «Комиссия» — «да» с причиной; остаток показываем с копейками."""
    from app.services import cash
    n = world["n"]
    cash.bank_balance(db, user=n, org_id=world["sadik"].id, actual=Decimal("64797.68"),
                      d=date.today() - timedelta(days=1), reason="утро")
    cash.withdraw(db, user=n, site_org_id=world["sadik"].id, account_org_id=world["sadik"].id,
                  amount=Decimal("64662"), d=date.today())
    _model(monkeypatch, {"kind": "bank", "bank_op": "balance", "balance": 1.35, "date": None, "sure": True})
    monkeypatch.setattr(bot_money, "_withdrew_today", lambda db, org, today: False)   # без снятия — спрашиваем
    svc.handle_update(db, _group(n, photo_id="bank3"))
    o = _offer(db, n)
    assert "остаток счёта садика 1,35" in o.text and "По записям 135,68" in o.text
    assert "Записал" in svc.handle_update(db, _private(n, "Камиссия"))
    rec = (db.query(Reconciliation).filter_by(kind="account", organization_id=world["sadik"].id)
           .order_by(Reconciliation.id.desc()).first())
    assert rec.actual_amount == Decimal("1.35") and rec.reason == "комиссия банка"


def test_short_non_answer_while_offer_open_is_silent_and_offer_stays(db, world, monkeypatch):
    """«Часть чека», «Овощи» при открытом «верно?» — не ответ: молчим, владельцу копия, вопрос жив."""
    n = world["n"]
    _model(monkeypatch, {"kind": "withdrawal", "amount": 25000, "date": "today", "sure": True})
    svc.handle_update(db, _group(n, "сняла 25 000"))
    assert svc.handle_update(db, _private(n, "Часть чека")) is None
    assert db.query(BotMessage).filter_by(kind="reply", user_id=n.id).count() == 0
    copies = db.query(BotMessage).filter_by(kind="owner_copy").all()
    assert len(copies) == 1 and "пишет: «Часть чека»" in copies[0].text
    assert _offer(db, n).status in ("sent", "logged")
    assert "Записал" in svc.handle_update(db, _private(n, "да", mid=53))


def test_on_hand_after_number_with_words_between(db, world):
    from app.services.bot import on_hand_amount
    assert on_hand_amount("51090остаток у меня наличка") == Decimal("51090")
    assert on_hand_amount("Остаток наличными 38322 Мунара") == Decimal("38322")
    n = world["n"]
    reply = svc.handle_update(db, _private(n, "51090остаток у меня наличка"))
    assert reply.startswith("Мунаратест, наличных у Мунаратест на руках 51 090")


def test_bank_doubt_in_private_is_silent_to_person(db, world, monkeypatch):
    """Модель приняла наличные за счёт, сомнение ушло владельцу — а ответ «остаток счёта 51 090»
    всё равно улетел Мунаре (24.09). В личке при сомнении — тишина."""
    from app.services import cash
    n = world["n"]
    cash.bank_balance(db, user=n, org_id=world["sadik"].id, actual=Decimal("136"),
                      d=date.today() - timedelta(days=1), reason="утро")
    _model(monkeypatch, {"kind": "balance", "amount": 51090, "date": "today", "sure": True})
    assert svc.handle_update(db, _private(n, "у меня осталось 51090 после закупа")) is None
    assert db.query(BotMessage).filter_by(kind="reply", user_id=n.id).count() == 0
    assert _offer(db, n) is None
    copies = db.query(BotMessage).filter_by(kind="owner_copy").all()
    assert len(copies) == 1 and "не спросил" in copies[0].text


def test_purchase_amount_in_private_points_to_existing_draft(db, world, monkeypatch):
    """«Ещё закуп 12770» в личку: чек на 12 770 уже в черновиках — говорим это; иначе просим фото."""
    n = world["n"]
    _model(monkeypatch, {"kind": "purchase", "amount": 12770, "supplier": "Торговый центр", "sure": True})
    svc.handle_update(db, _group(n, photo_id="rc12770"))
    assert svc.handle_update(db, _private(n, "Ещё закуп 12770")) == "Чек на 12 770 уже у Махабат на проверке."
    reply = svc.handle_update(db, _private(n, "закуп 5000", mid=54))
    assert reply.startswith("Мунаратест, закуп 5 000") and "фото чека" in reply


def test_one_word_answer_to_bot_question_closes_it(db, world, monkeypatch):
    """«Ответьте одним словом» → «Часть чека»: вопрос закрыт, владельцу — ответ, а не «не понял»."""
    n = world["n"]
    q = svc.send(db, n.tg_id, "Мунаратест, третье фото — список без цен. Это лист кухни, остаток склада или часть чека? "
                 "Ответьте одним словом.", "group_question", user_id=n.id)
    assert svc.handle_update(db, _private(n, "Часть чека")) is None
    assert q.status == "answered" and q.payload["answer"] == "Часть чека"
    copies = db.query(BotMessage).filter_by(kind="owner_copy").all()
    assert len(copies) == 1 and "ответил(а): «Часть чека»" in copies[0].text


def test_bot_paused_sends_nothing_to_people_but_owner(db, world, monkeypatch):
    from app.services import rules
    from app.models import User as _U
    owner = _U(name="Владелец тест-дн", role="owner", organization_id=world["sadik"].id, tg_id=900000000299)
    db.add(owner)
    db.flush()
    monkeypatch.setattr(svc, "OWNER_USER_ID", owner.id)
    monkeypatch.setattr(rules, "bot_paused", lambda db: True)
    monkeypatch.setenv(svc.TOKEN_ENV, "t")
    monkeypatch.setattr(svc.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен слать")))
    n = world["n"]
    m = svc.send(db, n.tg_id, "вопрос", "money_offer", user_id=n.id)
    assert m.status == "paused"
    monkeypatch.setattr(svc.httpx, "post", lambda *a, **k: type("R", (), {"status_code": 200, "text": ""})())
    assert svc.send(db, owner.tg_id, "строка", "owner_evening", user_id=owner.id).status == "sent"


def test_matching_figure_is_recorded_without_asking(db, world, monkeypatch):
    """Владелец 24.09: «по остаткам, если всё совпадает, переспрашивать не нужно»."""
    from app.services import cash
    n = world["n"]
    cash.withdraw(db, user=n, site_org_id=world["sadik"].id, account_org_id=world["sadik"].id,
                  amount=Decimal("38322"), d=date.today())
    reply = svc.handle_update(db, _private(n, "Остаток наличными 38322"))
    assert reply.startswith("Записал: наличных у Мунаратест на руках 38 322 — с записями сходится")
    rec = db.query(Reconciliation).filter_by(kind="pocket", subject_id=n.id).one()
    assert rec.actual_amount == Decimal("38322") and rec.delta == 0
    assert _offer(db, n).status == "answered"


def test_cash_figure_waits_for_receipt_drafts_then_settles(db, world, monkeypatch):
    """Мунара 24.09: «на руках 38 322» при 4 непроведённых чеках — «по записям 69 891» бессмысленно.
    Бот держит цифру, после проверки чеков сверяет: сошлось — записал, нет — один вопрос."""
    from app.services import cash
    n = world["n"]
    cash.withdraw(db, user=n, site_org_id=world["sadik"].id, account_org_id=world["sadik"].id,
                  amount=Decimal("64662"), d=date.today())
    _model(monkeypatch, {"kind": "purchase", "amount": 26340, "supplier": None, "sure": True})
    svc.handle_update(db, _group(n, photo_id="rc26340"))
    reply = svc.handle_update(db, _private(n, "Остаток наличными 38322"))
    assert reply == ("Принял 38 322. По записям у вас 64 662, закупы на 26 340 подготовлены: спишутся, когда Махабат "
                     "подтвердит их в системе, и станет 38 322 — как у вас.")
    assert _offer(db, n).status == "deferred"
    assert bot_money.settle_deferred(db, world["sadik"], date.today()) == []   # чек ещё висит
    from app.models import Receipt
    for rc in db.query(Receipt).filter_by(organization_id=world["sadik"].id).all():
        rc.ocr_status = "rejected"   # Махабат провела (здесь — сняла с проверки), карман 64 662
    done = bot_money.settle_deferred(db, world["sadik"], date.today())
    assert len(done) == 1
    o = _offer(db, n)
    assert o.status in ("sent", "logged") and "на руках 38 322. По записям 64 662, разница −26 340" in o.text


def test_owner_evening_line(db, world, monkeypatch):
    from app.services import cash
    n = world["n"]
    assert svc.owner_evening_text(db, world["sadik"], date.today()) == "Садик тест-дн: сошлось."
    cash.withdraw(db, user=n, site_org_id=world["sadik"].id, account_org_id=world["sadik"].id,
                  amount=Decimal("1000"), d=date.today())
    cash.recount(db, user=n, site_org_id=world["sadik"].id, pocket_user_id=n.id, actual=Decimal("900"),
                 d=date.today(), reason="x")
    svc.owner_copy(db, "заметка")
    text = svc.owner_evening_text(db, world["sadik"], date.today())
    assert text.startswith("Садик тест-дн: не сошлось — наличные Мунаратест −100. заметок бота 1")
    assert db.query(BotMessage).filter_by(kind="owner_copy").one().status == "noted"


def test_accountant_purchase_text_gets_form_link_not_receipt_request(db, world):
    """Махабат 24.09: «корм 1 200» без чека — она сама учётчик, ей форма, а не «пришлите фото»."""
    m = world["m"]
    reply = svc.handle_update(db, _private(m, "закуп корм 1200"))   # товар назван — черновик
    assert reply.startswith("Махабаттест, черновик закупа готов") and "/new/buy?receipt=" in reply
    reply = svc.handle_update(db, _group(m, "Закуп 1200"))          # только сумма — форма
    assert reply.startswith("Махабаттест, закуп 1 200 без чека — внесите через «Закуп»") and "/new/buy" in reply


def test_first_receipt_of_day_pings_checker_once(db, world, monkeypatch):
    m, n = world["m"], world["n"]
    _model(monkeypatch, {"kind": "purchase", "amount": 12770, "supplier": "Торговый центр", "sure": True})
    svc.handle_update(db, _group(n, photo_id="rcA"))
    ping = db.query(BotMessage).filter_by(kind="draft_ping", user_id=m.id).one()
    assert ping.text.startswith("Махабаттест, чек Мунаратест 12 770 ждёт проверки: ") and "/new/buy?receipt=" in ping.text
    _model(monkeypatch, {"kind": "purchase", "amount": 1600, "supplier": None, "sure": True})
    svc.handle_update(db, _group(n, photo_id="rcB", mid=2))
    assert db.query(BotMessage).filter_by(kind="draft_ping").count() == 1   # второй чек дня — молча


def test_handed_over_cash_is_transfer_not_on_hand(db, world, monkeypatch):
    """Мунара 24.09: «Вчерашний остаток наличными 5229сом отдала Махабату» — передача, не пересчёт."""
    from app.services.bot import on_hand_amount
    assert on_hand_amount("Вчерашний остаток наличными 5229сом отдала Махабату") is None
    n = world["n"]
    _model(monkeypatch, {"kind": "transfer", "amount": 5229, "who": None, "to": "Махабаттест",
                         "date": (date.today() - timedelta(days=1)).isoformat(), "sure": True})
    reply = svc.handle_update(db, _private(n, "Вчерашний остаток наличными 5229сом отдала Махабату"))
    assert reply == "Мунаратест, передача Мунаратест → Махабаттест 5 229, вчера. Верно? Да / нет"


def test_confirmed_transfer_is_posted_to_group(db, world, monkeypatch):
    """Владелец 24.09: передачи ведём в группе — вторая сторона видит запись."""
    n = world["n"]
    _model(monkeypatch, {"kind": "transfer", "amount": 5229, "who": None, "to": "Махабаттест",
                         "date": (date.today() - timedelta(days=1)).isoformat(), "sure": True})
    svc.handle_update(db, _private(n, "отдала Махабат 5229 вчера"))
    assert "Записал" in svc.handle_update(db, _private(n, "да", mid=55))
    g = db.query(BotMessage).filter_by(kind="group_transfer").one()
    assert g.chat_id == GROUP and g.text == "Мунаратест → Махабаттест 5 229, вчера — записал."


def test_purchase_text_becomes_draft(db, world, monkeypatch):
    """Махабат 24.09: «500 сом корм птицам 2 килограмма» — черновик закупа без фото, ей ссылка;
    Мунара «Кг кунжут 400 сом Мак 500 гр 275» — черновик, Махабат зовём."""
    from app.models import Receipt
    from app.services.bot import purchase_text
    assert purchase_text("500 сом корм птицам 2 килограмма")
    assert purchase_text("Кг кунжут400сом Мак 500гр 275")
    assert not purchase_text("Закуп 13570")
    assert not purchase_text("Остаток наличными 51090")
    assert not purchase_text("отдала Махабат 5229")
    m, n = world["m"], world["n"]
    reply = svc.handle_update(db, _group(m, "500 сом корм птицам 2 килограмма"))
    r = db.query(Receipt).filter_by(created_by=m.id).one()
    assert r.kind == "receipt" and r.file_path.endswith(".txt") and r.payload["text"].startswith("500 сом")
    assert reply == f"Махабаттест, черновик закупа готов — проверьте и запишите: {svc.public_url()}/new/buy?receipt={r.id}"
    reply = svc.handle_update(db, _group(n, "Кг кунжут400сом Мак 500гр 275", mid=3))
    assert reply == "Мунаратест, принял закуп текстом — Махабаттест проверит и запишет."
    assert db.query(Receipt).filter_by(created_by=n.id).count() == 1
    assert db.query(BotMessage).filter_by(kind="draft_ping", user_id=m.id).count() == 1


def test_small_expense_text_asks_author_and_yes_records_from_her_pocket(db, world, monkeypatch):
    """Владелец 24.09: «такси 200» — не черновик, а «верно?» автору; «да» — расход из её наличных."""
    from app.models import Transaction
    from app.services.bot import small_expense
    n, m = world["n"], world["m"]
    assert small_expense(db, "такси 200") == {"kind": "expense", "exp_kind": "delivery", "what": "такси",
                                              "amount": 200.0, "date": date.today()}
    assert small_expense(db, "доставка 150 сом")["what"] == "доставка"
    assert small_expense(db, "такси 1500") is None            # выше порога — черновик
    assert small_expense(db, "500 сом корм птицам 2 кг") is None   # товар — черновик
    assert small_expense(db, "отдала Махабат 200 на такси") is None
    assert svc.handle_update(db, _group(n, "такси 200")) is None   # в группе молчим
    o = _offer(db, n)
    assert o.text == "Мунаратест, такси 200 из наличных Мунаратест, сегодня. Верно? Да / нет"
    assert "Записал" in svc.handle_update(db, _private(n, "да"))
    t = db.query(Transaction).filter_by(organization_id=world["sadik"].id, type="expense").one()
    assert t.amount == Decimal("200") and t.paid_from_user_id == n.id and t.description == "такси"
    from app.models import ExpenseCategory
    assert db.get(ExpenseCategory, t.category_id).name == "Сервисные расходы"


def test_model_kinds_v2_are_routed(db, world, monkeypatch):
    """Промпт v2 (24.09): модель различает наличные на руках, закуп суммой, закуп с товарами, мелкий расход."""
    from app.models import Receipt
    n = world["n"]
    _model(monkeypatch, {"kind": "cash_on_hand", "amount": 51090, "sure": True})
    svc.handle_update(db, _group(n, "у меня осталось 51090 после закупа"))   # регексы молчат, читает модель
    o = _offer(db, n)
    assert o.payload["op"] == "recount" and "на руках 51 090" in o.text
    _model(monkeypatch, {"kind": "purchase_total", "amount": 13570, "sure": True})
    assert "фото чека" in svc.handle_update(db, _private(n, "потратила на покупки 13570"))
    _model(monkeypatch, {"kind": "purchase", "amount": 675, "sure": True})
    svc.handle_update(db, _group(n, "кунжут 400, мак 275", mid=4))
    assert db.query(Receipt).filter_by(created_by=n.id, kind="receipt").count() == 1
    _model(monkeypatch, {"kind": "expense", "amount": 300, "sure": True})
    reply = svc.handle_update(db, _private(n, "перевозка мебели 300", mid=60))
    assert reply.endswith("Верно? Да / нет") and "300 из наличных Мунаратест" in reply
    _model(monkeypatch, {"kind": "answer", "amount": None, "sure": True})
    assert svc.handle_update(db, _private(n, "хорошо 5", mid=61)) is None


def test_date_in_caption_is_not_amount():
    from datetime import date as _d
    from app.services import bot_group as grp
    import app.services.bot_group as g
    g.ask_model = lambda prompt, image=None, mime="image/jpeg", pdf=None: {"kind": "balance", "amount": 24.09, "sure": True}
    class _DB:
        def query(self, *a, **k):
            class Q:
                def filter(self, *a, **k): return self
                def all(self): return []
            return Q()
    info = grp.read_text(_DB(), "Карта да остаток 24.09.2026", None, _d.today())
    assert info["kind"] == "balance" and info["amount"] is None


def test_unrecognized_photo_called_receipt_becomes_draft(db, world, monkeypatch):
    """Махабат 24.09: фото документом с подписью «этот чек бот не подготовил?» — модель сказала
    «other», файл пропал. Теперь — черновик чека."""
    from app.models import Receipt
    m = world["m"]
    _model(monkeypatch, {"kind": "other", "sure": False})
    msg = {"message_id": 9, "chat": {"id": GROUP, "type": "supergroup"}, "from": {"id": m.tg_id},
           "caption": "этот чек бот не подготовил для ввода?",
           "document": {"file_id": "d9", "file_unique_id": "ud9", "file_name": "x.jpg", "mime_type": "image/jpeg"}}
    svc.handle_update(db, {"message": msg})
    r = db.query(Receipt).filter_by(created_by=m.id).one()
    assert r.kind == "receipt" and r.ocr_status == "pending"
