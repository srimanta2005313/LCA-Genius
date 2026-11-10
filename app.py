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

if __name__ == "__main__":
    socketio.run(app, debug=True)
