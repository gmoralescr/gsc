from flask import Flask, request, render_template
import pandas as pd
import statsmodels.api as sm
import numpy as np
import warnings

# Suppress warnings from statsmodels
warnings.simplefilter("ignore", category=RuntimeWarning)

app = Flask(__name__)

# ----------------------------------------------------------------
# 1. Load and preprocess dataset once when the app starts
# ----------------------------------------------------------------
df = pd.read_csv('FinalCookieSales.csv')

# Drop unwanted column
df = df.drop(columns=['date'], errors='ignore')

# Convert numeric columns
df['number_cases_sold'] = pd.to_numeric(df['number_cases_sold'], errors='coerce')
df['period'] = pd.to_numeric(df['period'], errors='coerce')
df['number_of_girls'] = pd.to_numeric(df['number_of_girls'], errors='coerce')

# Drop NaN values
df = df.dropna()

# Remove rows where number_cases_sold is 0
df = df[df['number_cases_sold'] > 0]

# Create squared period column
df['period_squared'] = df['period'] ** 2

# Calculate historical low & high for each troop-cookie type
historical_stats = df.groupby(['troop_id', 'cookie_type'])['number_cases_sold'].agg(['min', 'max']).reset_index()
historical_stats.columns = ['troop_id', 'cookie_type', 'historical_low', 'historical_high']

# Merge historical guardrails into the dataset
df = df.merge(historical_stats, on=['troop_id', 'cookie_type'], how='left')

# ----------------------------------------------------------------
# 2. Home route: display form (GET) and process predictions (POST)
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

    # If number_of_girls is zero, return zero predictions immediately
    if chosen_num_girls == 0:
        return f"<h1>Predictions for Troop: {chosen_troop}, Period: {chosen_period}</h1>" \
               f"<p>Number of Girls: {chosen_num_girls}</p>" \
               f"<p>Since there are zero girls, no cookies will be sold.</p>"

    # Filter the dataset for historical data
    df_troop = df[(df['troop_id'] == chosen_troop) & (df['period'] < chosen_period)]
    
    if df_troop.empty:
        return f"No historical data found for troop {chosen_troop} with periods before {chosen_period}.", 404

    # Group by cookie_type to train separate models
    predictions = []
    for cookie_type, group in df_troop.groupby('cookie_type'):
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
            
            # Ensure prediction is zero when number_of_girls is zero
            if chosen_num_girls == 0:
                predicted_cases = 0
            else:
                period_squared = chosen_period ** 2
                X_test = np.array([[1, chosen_period, period_squared, chosen_num_girls]])
                predicted_cases = model.predict(X_test)[0]

                # Apply historical guardrails
                historical_low = group['historical_low'].iloc[0]
                historical_high = group['historical_high'].iloc[0]
                predicted_cases = max(historical_low, min(predicted_cases, historical_high))

            predictions.append({
                "cookie_type": cookie_type,
                "predicted_cases": round(predicted_cases, 2)
            })
        except Exception:
            continue

    # Return results
    html_result = f"<h1>Predictions for Troop: {chosen_troop}, Period: {chosen_period}</h1>"
    html_result += f"<p>Number of Girls: {chosen_num_girls}</p>"
    if not predictions:
        html_result += "<p>No predictions available.</p>"
    else:
        html_result += "<ul>"
        for pred in predictions:
            html_result += f"<li>Cookie Type: {pred['cookie_type']} - Predicted Cases: {pred['predicted_cases']}</li>"
        html_result += "</ul>"

    return html_result


# ----------------------------------------------------------------
# 3. Run the app in debug mode for local testing
# ----------------------------------------------------------------
if __name__ == '__main__':
    app.run(debug=True)
