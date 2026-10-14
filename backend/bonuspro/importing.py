"""Strict canonical migration format; provider-specific exports require explicit mapping."""
import csv,io,re
from uuid import UUID
from hashlib import sha256
from bonuspro.adapters.postgres.ledger import fingerprint,MAX_POINTS


class ImportInvalid(ValueError):
    pass


def parse_csv(text):
    if not isinstance(text,str) or not text or len(text.encode("utf-8"))>60000:
        raise ImportInvalid("Файл должен быть UTF-8 CSV размером до 60 КБ.")
    text=text.lstrip("\ufeff")
    header=text.splitlines()[0] if text.splitlines() else ""
    delimiter=";" if header=="counterparty_id;bonus_balance" else ","
    reader=csv.DictReader(io.StringIO(text),delimiter=delimiter,strict=True)
    if reader.fieldnames!=["counterparty_id","bonus_balance"]:
        raise ImportInvalid("Нужны колонки counterparty_id и bonus_balance.")
    rows=[];seen=set()
    try:
        for index,row in enumerate(reader,start=2):
            if len(rows)>=100:raise ImportInvalid("За один импорт допускается до 100 клиентов.")
            if set(row)!={"counterparty_id","bonus_balance"} or any(v is None for v in row.values()):
                raise ImportInvalid("Некорректное число колонок в строке "+str(index)+".")
            try:identity=str(UUID(row["counterparty_id"].strip()))
            except (ValueError,AttributeError):raise ImportInvalid("Некорректный UUID клиента в строке "+str(index)+".") from None
            amount=row["bonus_balance"].strip()
            if not re.fullmatch(r"0|[1-9][0-9]{0,13}",amount) or int(amount)>MAX_POINTS:
                raise ImportInvalid("Баланс в строке "+str(index)+" должен быть целым неотрицательным числом.")
            if identity in seen:raise ImportInvalid("Один клиент указан несколько раз.")
            seen.add(identity);rows.append(dict(counterparty_id=identity,points=int(amount)))
    except csv.Error:
        raise ImportInvalid("Некорректная структура CSV.") from None
    if not rows:raise ImportInvalid("В файле нет клиентов.")
    rows.sort(key=lambda x:x["counterparty_id"])
    total=sum(x["points"] for x in rows)
    if total>MAX_POINTS:raise ImportInvalid("Общая сумма превышает допустимый предел.")
    return rows,fingerprint(rows),sha256(text.encode()).hexdigest()
