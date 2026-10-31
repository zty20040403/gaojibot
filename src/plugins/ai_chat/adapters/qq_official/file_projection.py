"""Read attachment delivery facts without granting upload or send authority."""
from __future__ import annotations



def apply_file_receipt(cursor, *, delivery, attempt, now):
    cursor.execute("""UPDATE qq_official_file_deliveries
        SET state='message_accepted',reason_code='',updated_at=? WHERE active_plan_id=?""",
        (now, delivery['plan_id']))


