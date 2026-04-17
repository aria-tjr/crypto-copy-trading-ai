"""
ML Filter — XGBoost classifier for signal quality prediction.
==============================================================
Layer 2: Predicts FULL / REDUCE_75 / REDUCE_50 / BLOCK for each signal.
Trained on historical trade outcomes, retrained weekly.
"""

import json
import logging
import os
import pickle
import time
from typing import Dict, List, Optional, Tuple

from .config import config, MLAction
from .signals import Signal, SignalDB

logger = logging.getLogger(__name__)


class MLFilter:
    """
    XGBoost classifier that scores incoming signals.
    
    Output classes:
    - FULL: Strong signal → full position size
    - REDUCE_75: Decent → 75% of base size
    - REDUCE_50: Weak → 50% of base size
    - BLOCK: Bad signal → skip entirely
    
    Until we have enough training data (>100 labeled trades),
    the filter runs in "passthrough" mode — returns FULL for
    everything and just logs what it would have done.
    """
    
    # Class labels
    LABELS = {0: "BLOCK", 1: "REDUCE_50", 2: "REDUCE_75", 3: "FULL"}
    LABEL_TO_IDX = {"BLOCK": 0, "REDUCE_50": 1, "REDUCE_75": 2, "FULL": 3}
    
    def __init__(self, db: SignalDB):
        self.db = db
        self.cfg = config.ml
        self.model = None
        self.scaler = None
        self.feature_names: List[str] = []
        self.model_path = os.path.join(self.cfg.model_dir, "xgb_filter.pkl")
        self.scaler_path = os.path.join(self.cfg.model_dir, "scaler.pkl")
        self.meta_path = os.path.join(self.cfg.model_dir, "model_meta.json")
        
        # Track if we're in passthrough mode
        self.passthrough = True
        self.min_training_samples = 100
        
        # Try to load existing model
        self._load_model()
    
    # ─── Prediction ──────────────────────────────────────────────────────
    
    def predict(self, features: Dict[str, float]) -> Tuple[str, float]:
        """
        Predict ML action and confidence for a signal.
        
        Returns: (action: str, confidence: float)
        """
        if self.passthrough or self.model is None:
            logger.info("🤖 ML Filter: passthrough mode (not enough training data)")
            return "FULL", 1.0
        
        try:
            # Ensure features are in correct order
            feature_vector = [features.get(name, 0.0) for name in self.feature_names]
            
            # Scale features
            if self.scaler:
                import numpy as np
                feature_vector = self.scaler.transform([feature_vector])[0]
            
            # Predict
            import numpy as np
            X = np.array([feature_vector])
            proba = self.model.predict_proba(X)[0]
            
            predicted_idx = int(np.argmax(proba))
            confidence = float(proba[predicted_idx])
            action = self.LABELS[predicted_idx]
            
            # If confidence is below minimum, block
            if confidence < self.cfg.min_confidence:
                logger.info(f"🤖 ML confidence {confidence:.2f} < {self.cfg.min_confidence} → BLOCK")
                return "BLOCK", confidence
            
            logger.info(f"🤖 ML prediction: {action} (confidence: {confidence:.2f})")
            return action, confidence
            
        except Exception as e:
            logger.error(f"ML prediction failed: {e}")
            return "FULL", 0.5  # Fail to passthrough
    
    # ─── Training ────────────────────────────────────────────────────────
    
    def train(self, force: bool = False) -> bool:
        """
        Train the XGBoost model on historical trade data.
        
        Labels are derived from realized PnL:
        - BLOCK: PnL < -1%
        - REDUCE_50: -1% ≤ PnL < +1%
        - REDUCE_75: +1% ≤ PnL < +3%
        - FULL: PnL ≥ +3%
        """
        try:
            import numpy as np
            from xgboost import XGBClassifier
            from sklearn.preprocessing import MinMaxScaler
            from sklearn.model_selection import train_test_split
        except ImportError:
            logger.warning("⚠️ xgboost/sklearn not installed. Install with: pip install xgboost scikit-learn")
            return False
        
        # Load historical trades
        trades = self.db.get_recent_trades(5000)
        labeled = [t for t in trades if t.status == "CLOSED" and t.realized_pnl != 0]
        
        if len(labeled) < self.min_training_samples and not force:
            logger.info(f"📊 Not enough training data ({len(labeled)}/{self.min_training_samples})")
            return False
        
        logger.info(f"🏋️ Training XGBoost on {len(labeled)} trades...")
        
        # Prepare training data
        # Features would need to be stored alongside each trade
        # For now, we use a simplified approach with available signal data
        X = []
        y = []
        
        for trade in labeled:
            # Get features from signal data (stored in DB)
            features = self._reconstruct_features(trade)
            if features:
                X.append(features)
                y.append(self._pnl_to_label(trade.realized_pnl, trade.entry_price))
        
        if len(X) < 20:
            logger.warning("Not enough valid training samples")
            return False
        
        X = np.array(X)
        y = np.array(y)
        
        # Split
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=self.cfg.test_size, stratify=y if len(set(y)) > 1 else None
        )
        
        # Scale
        self.scaler = MinMaxScaler()
        X_train = self.scaler.fit_transform(X_train)
        X_test = self.scaler.transform(X_test)
        
        # Train
        self.model = XGBClassifier(**self.cfg.xgb_params)
        self.model.fit(
            X_train, y_train,
            eval_set=[(X_test, y_test)],
            verbose=False
        )
        
        # Evaluate
        accuracy = self.model.score(X_test, y_test)
        logger.info(f"✅ Model trained — accuracy: {accuracy:.2%}")
        
        # Save
        self._save_model()
        self.passthrough = False
        
        return True
    
    def _pnl_to_label(self, pnl: float, entry_price: float) -> int:
        """Convert realized PnL to training label."""
        if entry_price <= 0:
            return 0
        pnl_pct = pnl / entry_price
        
        if pnl_pct < -0.01:
            return 0  # BLOCK
        elif pnl_pct < 0.01:
            return 1  # REDUCE_50
        elif pnl_pct < 0.03:
            return 2  # REDUCE_75
        else:
            return 3  # FULL
    
    def _reconstruct_features(self, trade: Signal) -> Optional[List[float]]:
        """Reconstruct feature vector from stored trade data."""
        # In production, features are stored alongside each trade
        # For now, return basic features from available data
        try:
            return [
                trade.entry_price,
                abs(trade.sl_pct),
                trade.tp_pcts[0] if trade.tp_pcts else 0.03,
                float(trade.leverage),
                trade.ml_confidence,
            ]
        except Exception:
            return None
    
    # ─── Model Persistence ───────────────────────────────────────────────
    
    def _save_model(self):
        """Save model, scaler, and metadata."""
        os.makedirs(self.cfg.model_dir, exist_ok=True)
        
        if self.model:
            with open(self.model_path, "wb") as f:
                pickle.dump(self.model, f)
        
        if self.scaler:
            with open(self.scaler_path, "wb") as f:
                pickle.dump(self.scaler, f)
        
        meta = {
            "trained_at": time.time(),
            "feature_names": self.feature_names,
            "passthrough": self.passthrough,
        }
        with open(self.meta_path, "w") as f:
            json.dump(meta, f, indent=2)
        
        logger.info(f"💾 Model saved to {self.cfg.model_dir}")
    
    def _load_model(self):
        """Load existing model if available."""
        try:
            if os.path.exists(self.model_path):
                with open(self.model_path, "rb") as f:
                    self.model = pickle.load(f)
                
                if os.path.exists(self.scaler_path):
                    with open(self.scaler_path, "rb") as f:
                        self.scaler = pickle.load(f)
                
                if os.path.exists(self.meta_path):
                    with open(self.meta_path, "r") as f:
                        meta = json.load(f)
                    self.feature_names = meta.get("feature_names", [])
                    self.passthrough = meta.get("passthrough", True)
                
                logger.info(f"📂 Loaded model from {self.cfg.model_dir} (passthrough={self.passthrough})")
            else:
                logger.info("📂 No existing model found — starting in passthrough mode")
        except Exception as e:
            logger.warning(f"Failed to load model: {e}")
            self.passthrough = True
