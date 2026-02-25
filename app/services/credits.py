import logging
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models import UserCredits, CreditTransaction

logger = logging.getLogger(__name__)

CREDIT_COSTS = {
    "ai_scoring": 1,
    "email_send": 0,
    "sms_send": 2,
    "lead_search": 0,
    "email_personalization": 1,
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

    def has_sufficient_credits(self, db: Session, user_id: int, action: str, count: int = 1) -> Tuple[bool, int, int]:
        cost = CREDIT_COSTS.get(action, 0) * count
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
        cost = CREDIT_COSTS.get(action, 0) * count
        if cost == 0:
            return True, self.get_balance(db, user_id)

        credits = db.query(UserCredits).filter_by(user_id=user_id).with_for_update().first()
        if not credits:
            credits = UserCredits(user_id=user_id, balance=0)
            db.add(credits)
            db.flush()

        balance = int(credits.balance or 0)
        if balance < cost:
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

    def add_credits(
        self,
        db: Session,
        user_id: int,
        amount: int,
        description: str,
        stripe_payment_intent_id: Optional[str] = None,
        stripe_checkout_session_id: Optional[str] = None,
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
            transaction_type="purchase",
            description=description,
            stripe_payment_intent_id=stripe_payment_intent_id,
            stripe_checkout_session_id=stripe_checkout_session_id,
            balance_after=new_balance,
        )
        db.add(transaction)
        db.commit()
        return new_balance

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
