from flask import Flask, redirect, url_for, render_template, flash, session, request, jsonify
import random
import json
from flask_sqlalchemy import SQLAlchemy
from form import Login, Signup
from flask_login import UserMixin, LoginManager, login_user, login_required, logout_user, current_user
from flask_mail import Mail, Message
from datetime import date, datetime
from werkzeug.security import generate_password_hash, check_password_hash
import pickle
import pandas as pd
from sqlalchemy import asc, desc
from flask_socketio import SocketIO, emit, join_room, leave_room
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import os
import uuid

app = Flask(__name__)

@app.route("/")
def landingpage():
    assessments = []
    pagination = None
    search_query = request.args.get('search', '')
    page = request.args.get('page', 1, type=int)
    
    if current_user.is_authenticated:
        query = Assessment.query.filter_by(user_id=current_user.id)
        
        if search_query:
            query = query.filter(db.or_(
                Assessment.project_name.ilike(f'%{search_query}%'),
                Assessment.metal_type.ilike(f'%{search_query}%')
            ))
            
        pagination = query.order_by(Assessment.created_at.desc()).paginate(page=page, per_page=10, error_out=False)
        assessments = pagination.items
        
    return render_template("landingpage.html", assessments=assessments, pagination=pagination, search_query=search_query)

@app.route("/predict", methods=['POST'])
@login_required
def predict():
    if not lca_model:
        flash("Model not loaded. Please contact admin.", "danger")
        return redirect(url_for('landingpage'))

    try:
        input_data = {
            'Company_Name': request.form.get('company_name') or current_user.company_name or "Unknown Company",
            'Project_Name': request.form.get('project_name') or f"{request.form.get('metal_type')} Analysis",
            'Metal_Type': request.form.get('metal_type'),
            'Production_Route': request.form.get('production_route'),
            'Energy_Consumption_MJ_per_ton': float(request.form.get('energy_consumption')),
            'Fuel_Type': request.form.get('fuel_type'),
            'Transport_Distance_km': float(request.form.get('transport_distance')),
            'Manufacturing_Process': request.form.get('manufacturing_process'),
            'End_of_Life_Option': request.form.get('end_of_life')
        }

        features = {}
        for col, le in encoders.items():
            if col in input_data:
                try:
                    features[col] = le.transform([input_data[col]])[0]
                except ValueError:
                    features[col] = 0 
        
        
        feature_vector = [
            features['Metal_Type'],
            features['Production_Route'],
            input_data['Energy_Consumption_MJ_per_ton'],
            features['Fuel_Type'],
            input_data['Transport_Distance_km'],
            features['Manufacturing_Process'],
            features['End_of_Life_Option']
        ]

        # Prediction
        prediction = lca_model.predict([feature_vector])[0]
        co2_emissions = round(prediction[0], 2)
        circularity_score = round(prediction[1], 2)

        results = {
            'CO2_Emissions_kg_per_ton': co2_emissions,
            'Circularity_Score': circularity_score
        }

        # Sustainability Analysis 
        sustainability_analysis = analyze_sustainability(co2_emissions, circularity_score)
        results['sustainability'] = sustainability_analysis
        
        # Benchmark Comparison
        INDUSTRY_AVG_CO2 = 1000
        results['benchmark_delta'] = round(((co2_emissions - INDUSTRY_AVG_CO2) / INDUSTRY_AVG_CO2) * 100, 1)

        # Generate Explanations
        explanations, top_drivers = generate_explanation(input_data, prediction)
        results['model_explanations'] = explanations
        results['top_drivers'] = top_drivers
        
        # What-If Simulation Suggestion
        what_if_msg = ""
        if co2_emissions > 1000:
             what_if_msg = "Switching to Renewable Energy or Secondary Route could potentially reduce emissions by 40-60%."
        elif circularity_score < 50:
             what_if_msg = "Increasing scrap/recycling input can boost circularity score significantly."
        results['what_if_insight'] = what_if_msg

        # Generate Recommendations
        recommendations = []
        if co2_emissions > 1000:
            recommendations.append("High CO2 Emissions detected. Consider switching to Renewable energy sources.")
            recommendations.append("Optimize transport logistics to reduce distance.")
        if circularity_score < 50:
            recommendations.append("Low Circularity Score. Increase recycled content in production.")
            recommendations.append("Explore 'Recycling' or 'Reuse' as End of Life options.")

        results['recommendations'] = recommendations

        results['breakdown'] = {
            'Extraction': round(co2_emissions * 0.4, 2),
            'Processing': round(co2_emissions * 0.3, 2),
            'Manufacturing': round(co2_emissions * 0.2, 2),
            'End_of_Life': round(co2_emissions * 0.1, 2)
        }

        # Save to DB
        new_assessment = Assessment(
            user_id=current_user.id,
            company_name=input_data['Company_Name'],
            project_name=input_data['Project_Name'],
            metal_type=input_data['Metal_Type'],
            production_route=input_data['Production_Route']
        )
        new_assessment.set_input(input_data)
        new_assessment.set_results(results)
        
        db.session.add(new_assessment)
        db.session.commit()

        return redirect(url_for('result_page', id=new_assessment.id))

    except Exception as e:
        flash(f"Error during prediction: {str(e)}", "danger")
        return redirect(url_for('landingpage'))
@app.route("/bulk_upload", methods=['POST'])
@login_required
def bulk_upload():
    if 'csv_file' not in request.files:
        flash("No file part", "danger")
        return redirect(url_for('landingpage'))
    
    file = request.files['csv_file']
    if file.filename == '':
        flash("No selected file", "danger")
        return redirect(url_for('landingpage'))
    
    if not file.filename.endswith('.csv'):
        flash("Please upload a valid CSV file.", "warning")
        return redirect(url_for('landingpage'))

    try:
        df = pd.read_csv(io.StringIO(file.stream.read().decode("UTF8")), sep=",")
        
        # Required columns for model
        required_cols = [
            'Project_Name', 'Metal_Type', 'Production_Route', 
            'Energy_Consumption', 'Fuel_Type', 'Transport_Distance', 
            'Manufacturing_Process', 'End_of_Life'
        ]
        
        # Check if columns exist
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            flash(f"Missing columns in CSV: {', '.join(missing)}", "danger")
            return redirect(url_for('landingpage'))
            
        # Store df rows in session or generate a task ID
        task_id = str(uuid.uuid4())
        
        # For simplicity, convert DF to dict to pass to background thread
        data_rows = df.to_dict('records')
        
        # Start background thread
        socketio.start_background_task(process_bulk_upload_background, task_id, current_user.id, current_user.company_name, data_rows)
        
        return render_template("bulk_progress.html", task_id=task_id)

    except Exception as e:
        flash(f"Error reading CSV: {str(e)}", "danger")
        return redirect(url_for('landingpage'))

def process_bulk_upload_background(task_id, user_id, company_name, data_rows):
    # Give the frontend client a few seconds to load the page and connect to the socket
    socketio.sleep(2.5)

    with app.app_context():
        processed_count = 0
        total_rows = len(data_rows)
        total_co2 = 0
        avg_circularity = 0
        
        new_assessments = []
        
        for index, row in enumerate(data_rows):
            try:
                # Prepare data for prediction
                input_data = {
                    'Company_Name': company_name,
                    'Project_Name': str(row['Project_Name']),
                    'Metal_Type': str(row['Metal_Type']),
                    'Production_Route': str(row['Production_Route']),
                    'Energy_Consumption_MJ_per_ton': float(row['Energy_Consumption']),
                    'Fuel_Type': str(row['Fuel_Type']),
                    'Transport_Distance_km': float(row['Transport_Distance']),
                    'Manufacturing_Process': str(row['Manufacturing_Process']),
                    'End_of_Life_Option': str(row['End_of_Life'])
                }

                features = {}
                for col, le in encoders.items():
                    # Map CSV column names to encoder names
                    mapping = {
                        'Metal_Type': 'Metal_Type',
                        'Production_Route': 'Production_Route',
                        'Fuel_Type': 'Fuel_Type',
                        'Manufacturing_Process': 'Manufacturing_Process',
                        'End_of_Life_Option': 'End_of_Life_Option'
                    }
                    
                    feat_key = None
                    for k, v in mapping.items():
                        if v == col: feat_key = k
                    
                    if feat_key and feat_key in input_data:
                        try:
                            features[col] = le.transform([input_data[feat_key]])[0]
                        except:
                            features[col] = 0
                
                feature_vector = [
                    features.get('Metal_Type', 0),
                    features.get('Production_Route', 0),
                    input_data['Energy_Consumption_MJ_per_ton'],
                    features.get('Fuel_Type', 0),
                    input_data['Transport_Distance_km'],
                    features.get('Manufacturing_Process', 0),
                    features.get('End_of_Life_Option', 0)
                ]

                prediction = lca_model.predict([feature_vector])[0]
                co2 = round(prediction[0], 2)
                circ = round(prediction[1], 2)
                
                sustainability_analysis = analyze_sustainability(co2, circ)
                
                results = {
                    'CO2_Emissions_kg_per_ton': co2,
                    'Circularity_Score': circ,
                    'sustainability': sustainability_analysis,
                    'breakdown': {
                        'Extraction': round(co2 * 0.4, 2),
                        'Processing': round(co2 * 0.3, 2),
                        'Manufacturing': round(co2 * 0.2, 2),
                        'End_of_Life': round(co2 * 0.1, 2)
                    }
                }

                # Create assessment object
                assessment = Assessment(
                    user_id=user_id,
                    company_name=input_data['Company_Name'],
                    project_name=input_data['Project_Name'],
                    metal_type=input_data['Metal_Type'],
                    production_route=input_data['Production_Route']
                )
                assessment.set_input(input_data)
                assessment.set_results(results)
                
                db.session.add(assessment)
                
                # We won't attach the new_assessments list to socket anymore, it will be pulled from DB or cached 
                # but to render bulk_result.html we need to pass something or just return successfully and redirect.
                
                processed_count += 1
                total_co2 += co2
                avg_circularity += circ
                
                # Emit progress every row (or every 10 rows if large)
                progress = int((processed_count / total_rows) * 100)
                socketio.emit('bulk_progress', {'progress': progress, 'current': processed_count, 'total': total_rows}, room=task_id)

                # Add a synthetic delay so the progress bar is visible even for small files
                socketio.sleep(0.5) 
            except Exception as e:
                print(f"Error processing row {index}: {e}")
                
        db.session.commit()
        
        # Save temporary summary to db or session, for simplicity we will just emit complete
        # and redirect user to history page where they can see the newly added items
        socketio.emit('bulk_complete', {'redirect_url': '/'}, room=task_id)


@socketio.on('join_task')
def on_join(data):
    task_id = data['task_id']
    join_room(task_id)
@app.route("/simulate", methods=['POST'])
@login_required
def simulate():
    if not lca_model:
        flash("Model not loaded. Please contact admin.", "danger")
        return redirect(url_for('landingpage'))

    metal_type = request.form.get('metal_type')
    
    # Metal-wise base data for simulation
    METAL_BASELINE = {
        "Aluminium": {
            "primary_energy": 15000,
            "secondary_energy": 3000,
            "emission_factor": 1.0
        },
        "Copper": {
            "primary_energy": 8000,
            "secondary_energy": 2000,
            "emission_factor": 0.7
        },
        "Steel": {
            "primary_energy": 6000,
            "secondary_energy": 1800,
            "emission_factor": 0.6
        }
    }

    base = METAL_BASELINE.get(metal_type, METAL_BASELINE["Steel"]) # Fallback to Steel
    
    # Define scenarios
    scenarios = [
        {
            'name': 'Scenario A: Primary + Coal',
            'Metal_Type': metal_type,
            'Production_Route': 'Primary',
            'Energy_Consumption_MJ_per_ton': float(base["primary_energy"]),
            'Fuel_Type': 'Coal',
            'Transport_Distance_km': 1000.0,
            'Manufacturing_Process': 'Smelting',
            'End_of_Life_Option': 'Landfill'
        },
        {
            'name': 'Scenario B: Secondary + Grid',
            'Metal_Type': metal_type,
            'Production_Route': 'Secondary',
            'Energy_Consumption_MJ_per_ton': float(base["secondary_energy"] * 1.5), # Slightly higher energy for "Transition" grid
            'Fuel_Type': 'Electricity',
            'Transport_Distance_km': 500.0,
            'Manufacturing_Process': 'Casting',
            'End_of_Life_Option': 'Recycling'
        },
        {
            'name': 'Scenario C: Secondary + Renewable',
            'Metal_Type': metal_type,
            'Production_Route': 'Secondary',
            'Energy_Consumption_MJ_per_ton': float(base["secondary_energy"]),
            'Fuel_Type': 'Renewable',
            'Transport_Distance_km': 200.0,
            'Manufacturing_Process': 'Refining',
            'End_of_Life_Option': 'Reuse'
        }
    ]

    simulation_results = []
    
    try:
        for scenario in scenarios:
            features = {}
            for col, le in encoders.items():
                if col in scenario:
                    try:
                        features[col] = le.transform([scenario[col]])[0]
                    except ValueError:
                        features[col] = 0
            
            feature_vector = [
                features['Metal_Type'],
                features['Production_Route'],
                scenario['Energy_Consumption_MJ_per_ton'],
                features['Fuel_Type'],
                scenario['Transport_Distance_km'],
                features['Manufacturing_Process'],
                features['End_of_Life_Option']
            ]

            prediction = lca_model.predict([feature_vector])[0]
            co2 = round(prediction[0], 2)
            circularity = round(prediction[1], 2)

            simulation_results.append({
                'name': scenario['name'],
                'CO2': co2,
                'Circularity': circularity,
                'Energy': scenario['Energy_Consumption_MJ_per_ton'],
                'Fuel': scenario['Fuel_Type'],
                'Route': scenario['Production_Route']
            })

        # Calculate Gains (Relative to Scenario A)
        baseline = simulation_results[0]
        for res in simulation_results:
            res['energy_saved'] = round(baseline['Energy'] - res['Energy'], 2)
            res['circularity_gain'] = round(res['Circularity'] - baseline['Circularity'], 2)
            res['co2_reduction_pct'] = round(((baseline['CO2'] - res['CO2']) / baseline['CO2']) * 100, 2) if baseline['CO2'] != 0 else 0

        return render_template("simulation_result.html", 
                               metal_type=metal_type, 
                               results=simulation_results)

    except Exception as e:
        flash(f"Error during simulation: {str(e)}", "danger")
        return redirect(url_for('landingpage'))
if __name__ == "__main__":
    socketio.run(app, debug=True)
