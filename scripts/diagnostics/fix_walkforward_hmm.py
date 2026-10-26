# Fix the walkforward_backtest.py to correctly handle HMM return values

with open('walkforward_backtest.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the HMM usage - fit_and_predict returns (probs, states), not (states, probs)
old_hmm = '''    # Train HMM on training window
    logger.info("Training HMM on macro data...")
    macro_train_dates = pd.date_range(fold["train_start"], fold["train_end"], freq="B")
    # Generate synthetic macro data for training (in production, load real macro data)
    np.random.seed(42)
    macro_pcs = pd.DataFrame({
        "PC1": np.random.normal(0, 1, len(macro_train_dates)),
        "PC2": np.random.normal(0, 1, len(macro_train_dates)),
        "PC3": np.random.normal(0, 1, len(macro_train_dates)),
    }, index=macro_train_dates)
    vix_train = pd.Series(np.abs(np.random.normal(16, 3, len(macro_train_dates))), index=macro_train_dates)
    
    hmm = FactorHMMv3(min_dwell_days=10)
    states, probs = hmm.fit_and_predict(macro_pcs, vix_train)
    states = hmm.apply_dwell_filter(states)'''

new_hmm = '''    # Train HMM on training window
    logger.info("Training HMM on macro data...")
    macro_train_dates = pd.date_range(fold["train_start"], fold["train_end"], freq="B")
    # Generate synthetic macro data for training (in production, load real macro data)
    np.random.seed(42)
    macro_pcs = pd.DataFrame({
        "PC1": np.random.normal(0, 1, len(macro_train_dates)),
        "PC2": np.random.normal(0, 1, len(macro_train_dates)),
        "PC3": np.random.normal(0, 1, len(macro_train_dates)),
    }, index=macro_train_dates)
    vix_train = pd.Series(np.abs(np.random.normal(16, 3, len(macro_train_dates))), index=macro_train_dates)
    
    hmm = FactorHMMv3(min_dwell_days=10)
    probs, states = hmm.fit_and_predict(macro_pcs, vix_train)
    # states already has dwell filter applied'''

content = content.replace(old_hmm, new_hmm)

with open('walkforward_backtest.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Fixed HMM return value handling')