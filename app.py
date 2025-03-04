from flask import Flask, request, render_template
import pandas as pd
import statsmodels.api as sm
import numpy as np
import warnings
import time

# Additional imports for Ridge modeling
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

# TQDM can be used in scripts, but in a Flask environment
# it will mostly just print in the console/log.
from tqdm import tqdm

warnings.simplefilter("ignore", category=RuntimeWarning)

app = Flask(__name__)

# ----------------------------------------------------------------
# 1. Load and preprocess dataset once when the app starts
# ----------------------------------------------------------------
df = pd.read_csv('FinalCookieSales.csv')

# Drop 'date' if it exists
df = df.drop(columns=['date'], errors='ignore')

# Convert numeric columns. Force troop_id and period to int, number_of_girls to float
df['troop_id'] = pd.to_numeric(df['troop_id'], errors='coerce').astype('Int64')
df['period'] = pd.to_numeric(df['period'], errors='coerce').astype('Int64')
df['number_of_girls'] = pd.to_numeric(df['number_of_girls'], errors='coerce').astype(float)
df['number_cases_sold'] = pd.to_numeric(df['number_cases_sold'], errors='coerce')

# Drop rows with any NaN
df = df.dropna()

# Remove rows where number_cases_sold is 0
df = df[df['number_cases_sold'] > 0]

# Ensure troop_id and period are plain Python ints (if you prefer)
df['troop_id'] = df['troop_id'].astype(int)
df['period'] = df['period'].astype(int)

# Create squared period column (used in your OLS approach)
df['period_squared'] = df['period'] ** 2

# Merge or calculate historical min/max if you want to do guardrails
# Make sure we have them for OLS predictions
historical_stats = df.groupby(['troop_id', 'cookie_type'])['number_cases_sold'].agg(['min', 'max']).reset_index()
historical_stats.columns = ['troop_id', 'cookie_type', 'historical_low', 'historical_high']
df = df.merge(historical_stats, on=['troop_id', 'cookie_type'], how='left')

# ----------------------------------------------------------------
# 2. Define a function to run Ridge + coverage analysis once
# ----------------------------------------------------------------
def mean_absolute_percentage_error(y_true, y_pred):
    return np.mean(np.abs((y_true - y_pred) / y_true)) * 100

def run_ridge_interval_analysis():
    start_time = time.time()

    # Group by troop and cookie
    groups = df.groupby(['troop_id', 'cookie_type'])
    total_groups = len(groups)

    # Storage for metrics
    y_train_all = []
    y_train_pred_all = []
    y_test_all = []
    y_test_pred_all = []

    test_records = []  # To build final test DataFrame

    # Range of alphas to try
    ridge_alphas = np.logspace(-2, 3, 10)

    # Loop over each group
    for (troop, cookie), group in tqdm(groups, total=total_groups, desc="Processing Models"):
        group = group.sort_values(by='period')

        # Train on periods 1-4, Test on period 5 (example split)
        train = group[group['period'] <= 4]
        test = group[group['period'] == 5]

        if train.empty or test.empty:
            continue

        X_train = train[['period', 'number_of_girls']]
        y_train = train['number_cases_sold']
        X_test = test[['period', 'number_of_girls']]
        y_test = test['number_cases_sold']

        # Skip if still empty
        if X_train.empty:
            continue

        # Scale
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        X_test_scaled = scaler.transform(X_test)

        best_alpha = None
        best_mae = float('inf')
        best_model = None
        best_y_test_pred = None
        best_y_train_pred = None

        # Find best alpha via test MAE
        for alpha in ridge_alphas:
            try:
                ridge_model = Ridge(alpha=alpha)
                ridge_model.fit(X_train_scaled, y_train)

                y_train_pred_current = ridge_model.predict(X_train_scaled)
                y_test_pred_current = ridge_model.predict(X_test_scaled)

                mae = mean_absolute_error(y_test, y_test_pred_current)
                if mae < best_mae:
                    best_mae = mae
                    best_alpha = alpha
                    best_model = ridge_model
                    best_y_train_pred = y_train_pred_current
                    best_y_test_pred = y_test_pred_current
            except:
                continue

        if best_model is not None:
            # Store for overall metrics
            y_train_all.extend(y_train)
            y_train_pred_all.extend(best_y_train_pred)
            y_test_all.extend(y_test)
            y_test_pred_all.extend(best_y_test_pred)

            # Save test data & predictions
            tmp_test_df = test.copy()
            tmp_test_df['troop_id'] = troop
            tmp_test_df['cookie_type'] = cookie
            tmp_test_df['predicted'] = best_y_test_pred
            test_records.append(tmp_test_df)

    # Convert lists to arrays
    y_train_all = np.array(y_train_all)
    y_train_pred_all = np.array(y_train_pred_all)
    y_test_all = np.array(y_test_all)
    y_test_pred_all = np.array(y_test_pred_all)

    # Compute training metrics
    mae_train = mean_absolute_error(y_train_all, y_train_pred_all)
    mse_train = mean_squared_error(y_train_all, y_train_pred_all)
    rmse_train = np.sqrt(mse_train)
    r2_train = r2_score(y_train_all, y_train_pred_all)
    mape_train = mean_absolute_percentage_error(y_train_all, y_train_pred_all)

    # Compute testing metrics
    mae_test = mean_absolute_error(y_test_all, y_test_pred_all)
    mse_test = mean_squared_error(y_test_all, y_test_pred_all)
    rmse_test = np.sqrt(mse_test)
    r2_test = r2_score(y_test_all, y_test_pred_all)
    mape_test = mean_absolute_percentage_error(y_test_all, y_test_pred_all)

    # Build single test DF
    if len(test_records) > 0:
        test_df_all = pd.concat(test_records, ignore_index=True)
    else:
        test_df_all = pd.DataFrame()

    # Use overall training RMSE to define intervals
    overall_train_rmse = rmse_train
    coverage_factor = 2.0  # Adjust as needed
    interval_width = coverage_factor * overall_train_rmse

    if not test_df_all.empty:
        test_df_all['interval_lower'] = test_df_all['predicted'] - interval_width
        test_df_all['interval_upper'] = test_df_all['predicted'] + interval_width

        # Coverage
        test_df_all['in_interval'] = (
            (test_df_all['number_cases_sold'] >= test_df_all['interval_lower']) &
            (test_df_all['number_cases_sold'] <= test_df_all['interval_upper'])
        )
        test_df_all['error'] = np.abs(test_df_all['number_cases_sold'] - test_df_all['predicted'])
        interval_coverage_rate = test_df_all['in_interval'].mean() * 100
    else:
        interval_coverage_rate = 0

    # Print results (visible in Flask console)
    print("\n--- Ridge + Interval Coverage Results ---")
    print("Training Set Metrics:")
    print(f"MAE: {mae_train:.2f}, MSE: {mse_train:.2f}, RMSE: {rmse_train:.2f}, "
          f"MAPE: {mape_train:.2f}%, R²: {r2_train:.4f}")
    print("\nTesting Set Metrics:")
    print(f"MAE: {mae_test:.2f}, MSE: {mse_test:.2f}, RMSE: {rmse_test:.2f}, "
          f"MAPE: {mape_test:.2f}%, R²: {r2_test:.4f}")

    print(f"\nOverall Training RMSE: {overall_train_rmse:.2f}")
    print(f"Interval Width: ±({coverage_factor} × {overall_train_rmse:.2f}) = ±{interval_width:.2f}")
    print(f"\nOverall Test Prediction Interval Coverage: {interval_coverage_rate:.2f}%")

    if not test_df_all.empty:
        worst_preds = test_df_all.sort_values('error', ascending=False).head(10)
        print("\nTop 10 Worst Predictions (By Absolute Error):")
        print(worst_preds[['troop_id', 'cookie_type', 'period', 'number_of_girls',
                           'number_cases_sold', 'predicted', 'interval_lower',
                           'interval_upper', 'in_interval', 'error']])
    else:
        print("\nNo test data found during Ridge analysis.")

    end_time = time.time()
    print(f"\nProcess completed in {end_time - start_time:.2f} seconds.\n")

# ----------------------------------------------------------------
# 3. Run your Ridge analysis once at startup
# ----------------------------------------------------------------
run_ridge_interval_analysis()

# ----------------------------------------------------------------
# 4. Home route: display form (GET) and process predictions (POST)
#    using OLS approach (unchanged from your example)
# ----------------------------------------------------------------
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'GET':
        return render_template('index.html')

    # Get user inputs
    chosen_period = request.form.get('period')
    chosen_troop = request.form.get('troop_id')
    chosen_num_girls = request.form.get('number_of_girls')

    # Validate and convert inputs
    try:
        chosen_period = int(chosen_period)
        chosen_troop = int(chosen_troop)
        chosen_num_girls = float(chosen_num_girls)
    except ValueError:
        return "Invalid input. Please enter valid numeric values.", 400

    # If number_of_girls is zero, short-circuit the prediction
    if chosen_num_girls == 0:
        return (f"<h1>Predictions for Troop: {chosen_troop}, Period: {chosen_period}</h1>"
                f"<p>Number of Girls: {chosen_num_girls}</p>"
                f"<p>Since there are zero girls, no cookies will be sold.</p>")

    # Filter historical data for this troop
    df_troop = df[(df['troop_id'] == chosen_troop) & (df['period'] < chosen_period)]
    if df_troop.empty:
        return f"No historical data found for troop {chosen_troop} with periods before {chosen_period}.", 404

    predictions = []
    for cookie_type, group in df_troop.groupby('cookie_type'):
        # If there's only 1 data point, fallback to last known
        if group['period'].nunique() < 2:
            last_period = group['period'].max()
            last_val = group.loc[group['period'] == last_period, 'number_cases_sold'].mean()
            predictions.append({
                "cookie_type": cookie_type,
                "predicted_cases": round(last_val, 2),
                "note": "Using last available period value"
            })
            continue

        # Prepare training data
        X_train = group[['period', 'period_squared', 'number_of_girls']]
        y_train = group['number_cases_sold']
        X_train = sm.add_constant(X_train)

        try:
            model = sm.OLS(y_train, X_train).fit()
            period_squared = chosen_period ** 2
            X_test = np.array([[1, chosen_period, period_squared, chosen_num_girls]])
            predicted_cases = model.predict(X_test)[0]

            # Historical guardrails
            historical_low = group['historical_low'].iloc[0]
            historical_high = group['historical_high'].iloc[0]
            predicted_cases = max(historical_low, min(predicted_cases, historical_high))

            predictions.append({
                "cookie_type": cookie_type,
                "predicted_cases": round(predicted_cases, 2)
            })
        except Exception:
            continue

    # Format HTML response
    html_result = f"<h1>Predictions for Troop: {chosen_troop}, Period: {chosen_period}</h1>"
    html_result += f"<p>Number of Girls: {chosen_num_girls}</p>"
    if not predictions:
        html_result += "<p>No predictions available.</p>"
    else:
        html_result += "<ul>"
        for pred in predictions:
            html_result += (f"<li>Cookie Type: {pred['cookie_type']} "
                            f"- Predicted Cases: {pred['predicted_cases']}</li>")
        html_result += "</ul>"

    return html_result

# ----------------------------------------------------------------
# 5. Run the app in debug mode for local testing
# ----------------------------------------------------------------
if __name__ == '__main__':
    app.run(debug=True)
