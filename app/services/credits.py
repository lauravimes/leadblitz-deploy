import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import UserCredits, CreditTransaction, Payment

logger = logging.getLogger(__name__)

# Single source of truth for what each action costs. Rendered on /credits and used
# by deduct_credits — keep them in sync by only ever editing this table.
CREDIT_COSTS = {
    "ai_scoring": 1,
    "email_send": 0,
    "sms_send": 0,
    "lead_search": 1,
    "email_personalization": 1,
    "hunter_enrichment": 2,
}

# Human labels for the costs table on /credits
CREDIT_COST_LABELS = {
    "lead_search": "Lead search (per page of 20 results)",
    "ai_scoring": "AI website score",
    "email_personalization": "AI-written email",
    "hunter_enrichment": "Hunter.io email lookup",
    "email_send": "Email send",
    "sms_send": "SMS send (Twilio charges apply)",
}


class CreditManager:

    def get_or_create(self, db: Session, user_id: int) -> UserCredits:
        credits = db.query(UserCredits).filter_by(user_id=user_id).first()
        if not credits:
            credits = UserCredits(user_id=user_id, balance=0)
            db.add(credits)
            db.flush()
        return credits

    def get_user_credits(self, db: Session, user_id: int) -> Dict:
        credits = self.get_or_create(db, user_id)
        return {
            "balance": credits.balance,
            "total_purchased": credits.total_purchased,
            "total_used": credits.total_used,
            "stripe_customer_id": credits.stripe_customer_id,
        }

    def get_balance(self, db: Session, user_id: int) -> int:
        return int(self.get_user_credits(db, user_id)["balance"] or 0)

    def cost_of(self, action: str, count: int = 1) -> int:
        return CREDIT_COSTS.get(action, 0) * count

    def has_sufficient_credits(self, db: Session, user_id: int, action: str, count: int = 1) -> Tuple[bool, int, int]:
        cost = self.cost_of(action, count)
        balance = self.get_balance(db, user_id)
        return balance >= cost, balance, cost

    def deduct_credits(
        self,
        db: Session,
        user_id: int,
        action: str,
        count: int = 1,
        description: Optional[str] = None,
    ) -> Tuple[bool, int]:
        cost = self.cost_of(action, count)
        if cost == 0:
            return True, self.get_balance(db, user_id)

        credits = db.query(UserCredits).filter_by(user_id=user_id).with_for_update().first()
        if not credits:
            credits = UserCredits(user_id=user_id, balance=0)
            db.add(credits)
            db.flush()

        balance = int(credits.balance or 0)
        if balance < cost:
            db.rollback()
            return False, balance

        credits.balance = balance - cost
        credits.total_used = int(credits.total_used or 0) + cost
        new_balance = credits.balance

        transaction = CreditTransaction(
            user_id=user_id,
            amount=-cost,
            transaction_type="usage",
            description=description or f"{action} x{count}",
            balance_after=new_balance,
        )
        db.add(transaction)
        db.commit()
        return True, new_balance

    def refund_credits(
        self,
        db: Session,
        user_id: int,
        action: str,
        count: int = 1,
        description: Optional[str] = None,
    ) -> int:
        """Return credits previously taken by deduct_credits (e.g. the paid-for
        operation failed). Returns the new balance."""
        cost = self.cost_of(action, count)
        if cost == 0:
            return self.get_balance(db, user_id)

        credits = db.query(UserCredits).filter_by(user_id=user_id).with_for_update().first()
        if not credits:
            credits = UserCredits(user_id=user_id, balance=0)
            db.add(credits)
            db.flush()

        credits.balance = int(credits.balance or 0) + cost
        credits.total_used = max(0, int(credits.total_used or 0) - cost)
        new_balance = credits.balance

        db.add(CreditTransaction(
            user_id=user_id,
            amount=cost,
            transaction_type="refund",
            description=description or f"Refund: {action} x{count}",
            balance_after=new_balance,
        ))
        db.commit()
        return new_balance

    def add_credits(
        self,
        db: Session,
        user_id: int,
        amount: int,
        description: str,
        stripe_payment_intent_id: Optional[str] = None,
        stripe_checkout_session_id: Optional[str] = None,
        transaction_type: str = "purchase",
    ) -> int:
        credits = db.query(UserCredits).filter_by(user_id=user_id).with_for_update().first()
        if not credits:
            credits = UserCredits(user_id=user_id, balance=0)
            db.add(credits)
            db.flush()

        credits.balance = int(credits.balance or 0) + amount
        credits.total_purchased = int(credits.total_purchased or 0) + amount
        new_balance = credits.balance

        transaction = CreditTransaction(
            user_id=user_id,
            amount=amount,
            transaction_type=transaction_type,
            description=description,
            stripe_payment_intent_id=stripe_payment_intent_id,
            stripe_checkout_session_id=stripe_checkout_session_id,
            balance_after=new_balance,
        )
        db.add(transaction)
        db.commit()
        return new_balance

    def record_purchase(
        self,
        db: Session,
        user_id: int,
        credits_amount: int,
        plan_name: str,
        amount_cents: int,
        checkout_session_id: str,
        stripe_event_id: Optional[str] = None,
        stripe_customer_id: Optional[str] = None,
    ) -> bool:
        """Idempotently credit a completed Stripe Checkout session.

        The CreditTransaction row (unique on stripe_checkout_session_id) is inserted
        in the same transaction as the balance update, so two concurrent callers
        (Stripe webhook + the /credits/success redirect) cannot both grant. Returns
        True if credits were granted now, False if this session was already
        processed.
        """
        if not checkout_session_id or credits_amount <= 0:
            return False

        try:
            credits = db.query(UserCredits).filter_by(user_id=user_id).with_for_update().first()
            if not credits:
                credits = UserCredits(user_id=user_id, balance=0)
                db.add(credits)
                db.flush()

            credits.balance = int(credits.balance or 0) + credits_amount
            credits.total_purchased = int(credits.total_purchased or 0) + credits_amount
            if stripe_customer_id and not credits.stripe_customer_id:
                credits.stripe_customer_id = stripe_customer_id

            db.add(CreditTransaction(
                user_id=user_id,
                amount=credits_amount,
                transaction_type="purchase",
                description=f"Purchased {plan_name} ({credits_amount} credits)",
                stripe_checkout_session_id=checkout_session_id,
                stripe_event_id=stripe_event_id,
                balance_after=credits.balance,
            ))
            db.add(Payment(
                user_id=user_id,
                stripe_session_id=checkout_session_id,
                amount_cents=amount_cents,
                credits_purchased=credits_amount,
                plan_name=plan_name,
                status="completed",
            ))
            db.commit()
            logger.info("[STRIPE] Granted %s credits to user %s for %s", credits_amount, user_id, checkout_session_id)
            return True
        except IntegrityError:
            db.rollback()
            logger.info("[STRIPE] Session %s already processed — skipping", checkout_session_id)
            return False

    def set_stripe_customer_id(self, db: Session, user_id: int, stripe_customer_id: str):
        credits = self.get_or_create(db, user_id)
        credits.stripe_customer_id = stripe_customer_id
        db.commit()

    def get_transaction_history(self, db: Session, user_id: int, limit: int = 50) -> List[Dict]:
        transactions = (
            db.query(CreditTransaction)
            .filter_by(user_id=user_id)
            .order_by(CreditTransaction.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": t.id,
                "amount": t.amount,
                "type": t.transaction_type,
                "description": t.description,
                "balance_after": t.balance_after,
                "created_at": t.created_at.isoformat() if t.created_at else "",
            }
            for t in transactions
        ]

    def check_duplicate_session(self, db: Session, checkout_session_id: str) -> bool:
        existing = (
            db.query(CreditTransaction)
            .filter_by(stripe_checkout_session_id=checkout_session_id)
            .first()
        )
        return existing is not None


credit_manager = CreditManager()
