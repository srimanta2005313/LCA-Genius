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

@app.route("/export_history")
@login_required
def export_history():
    assessments = Assessment.query.filter_by(user_id=current_user.id).order_by(Assessment.created_at.desc()).all()
    
    if not assessments:
        flash("No assessments found to export.", "warning")
        return redirect(url_for('landingpage'))

    data = []
    for a in assessments:
        results = a.get_results()
        row = {
            'Date': a.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'Company Name': a.company_name,
            'Project Name': a.project_name,
            'Metal Type': a.metal_type,
            'Production Route': a.production_route,
            'CO2 Emissions (kg/ton)': results.get('CO2_Emissions_kg_per_ton', ''),
            'Circularity Score': results.get('Circularity_Score', ''),
            'Verdict': results.get('sustainability', {}).get('verdict', '')
        }
        data.append(row)

    df = pd.DataFrame(data)
    
    # Create an in-memory CSV string
    output = io.StringIO()
    df.to_csv(output, index=False)
    
    # Send the response
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=assessment_history.csv"}
    )
    
@app.route("/result/<int:id>")
@login_required
def result_page(id):
    assessment = Assessment.query.get_or_404(id)
    if assessment.user_id != current_user.id:
        flash("Unauthorized access", "danger")
        return redirect(url_for('landingpage'))
    
    return render_template("result.html", assessment=assessment, input=assessment.get_input(), results=assessment.get_results())


@app.route("/delete_assessment/<int:id>", methods=['POST'])
@login_required
def delete_assessment(id):
    assessment = Assessment.query.get_or_404(id)
    if assessment.user_id != current_user.id:
        flash("Unauthorized access", "danger")
        return redirect(url_for('landingpage'))
    
    db.session.delete(assessment)
    db.session.commit()
    flash("Assessment deleted successfully.", "success")
    return redirect(url_for('landingpage'))


@app.route("/download_pdf/<int:id>")
@login_required
def download_pdf(id):
    assessment = Assessment.query.get_or_404(id)
    if assessment.user_id != current_user.id:
        return "Unauthorized", 403

    input_data = assessment.get_input()
    results = assessment.get_results()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)

    pdf.cell(200, 10, txt="LCA Analysis Report", ln=1, align="C")
    pdf.ln(10)

    pdf.set_font("Arial", 'B', size=12)
    pdf.cell(200, 10, txt="Input Parameters:", ln=1)
    pdf.set_font("Arial", size=10)
    pdf.cell(200, 10, txt=f"Company: {assessment.company_name}", ln=1)
    if assessment.project_name:
        pdf.cell(200, 10, txt=f"Project: {assessment.project_name}", ln=1)
    
    pdf.ln(5)
    
    pdf.ln(10)
    pdf.set_font("Arial", 'B', size=12)
    pdf.cell(200, 10, txt="Results:", ln=1)
    pdf.set_font("Arial", size=12)
    pdf.cell(200, 10, txt=f"CO2 Emissions: {results['CO2_Emissions_kg_per_ton']} kg/ton", ln=1)
    pdf.cell(200, 10, txt=f"Circularity Score: {results['Circularity_Score']}", ln=1)

    # Pie Chart Generation
    breakdown = results.get('breakdown', {})
    if breakdown:
        labels = list(breakdown.keys())
        sizes = list(breakdown.values())
        
        plt.figure(figsize=(6, 6))
        plt.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=140, colors=['#ff9999','#66b3ff','#99ff99','#ffcc99'])
        plt.title('Emissions Breakdown')
        plt.axis('equal') 
        
        # Save temp image
        img_filename = f"temp_chart_{os.urandom(8).hex()}.png"
        img_path = os.path.join(app.root_path, 'static', img_filename)
        plt.savefig(img_path, format='png', bbox_inches='tight')
        plt.close()
        
        pdf.ln(10)
        pdf.image(img_path, x=50, w=110)
        
        # Cleanup
        try:
            os.remove(img_path)
        except:
            pass
            
    pdf.ln(10)
    pdf.set_font("Arial", 'B', size=12)
    pdf.cell(200, 10, txt="Recommendations:", ln=1)
    pdf.set_font("Arial", size=12)
    for rec in results.get('recommendations', []):
        pdf.multi_cell(0, 10, txt=f"- {rec}")

    response = io.BytesIO(pdf.output(dest='S').encode('latin-1'))
    
    from flask import send_file
    return send_file(
        response,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'LCA_Report_{id}.pdf'
    )

@app.route('/admin')
def admin_dashboard():
    if not session.get('admin_logged_in'):
        flash("Admin access required.", "danger")
        return redirect(url_for('logins'))

    # 1. Metal Type Distribution
    from sqlalchemy import func
    metal_counts = db.session.query(Assessment.metal_type, func.count(Assessment.id)).group_by(Assessment.metal_type).all()
    metal_labels = [m[0] for m in metal_counts]
    metal_values = [m[1] for m in metal_counts]

    # 2. Get all assessments to process trends and high impact filters
    all_assessments = Assessment.query.all()
    
    trends_dict = {} # {date: [values]}
    company_monthly_stats = {} # {(company, month): {'co2': [], 'circ': []}}
    
    total_co2 = 0
    total_circ = 0
    total_count = len(all_assessments)

    for a in all_assessments:
        res = a.get_results()
        co2 = res.get('CO2_Emissions_kg_per_ton', 0)
        circ = res.get('Circularity_Score', 0)
        
        # Aggregation for trends
        date_str = a.created_at.strftime('%Y-%m-%d')
        if date_str not in trends_dict:
            trends_dict[date_str] = []
        trends_dict[date_str].append(co2)
        
        # Aggregation for Company Monthly Stats
        # Month format: "March 2026"
        month_str = a.created_at.strftime('%B %Y')
        comp_key = (a.company_name, month_str)
        if comp_key not in company_monthly_stats:
            company_monthly_stats[comp_key] = {'co2': [], 'circ': [], 'detailed_projects': []}
        company_monthly_stats[comp_key]['co2'].append(co2)
        company_monthly_stats[comp_key]['circ'].append(circ)
        company_monthly_stats[comp_key]['detailed_projects'].append({
            'name': a.project_name or "Project Analysis",
            'co2': co2,
            'circ': circ
        })
        
        total_co2 += co2
        total_circ += circ

    # Process company monthly stats into a flat list for the template
    aggregated_companies = []
    for (name, month), data in company_monthly_stats.items():
        avg_co2 = round(sum(data['co2']) / len(data['co2']), 2)
        avg_circ = round(sum(data['circ']) / len(data['circ']), 2)
        aggregated_companies.append({
            'company': name,
            'co2': avg_co2,
            'circ': avg_circ,
            'month': month,
            'projects': data['detailed_projects']
        })

    # Sort by month descending, then company name
    aggregated_companies.sort(key=lambda x: (x['month'], x['company']), reverse=True)

    # Process trends data
    sorted_dates = sorted(trends_dict.keys())
    avg_co2_per_day = [round(sum(trends_dict[d]) / len(trends_dict[d]), 2) for d in sorted_dates]

    stats = {
        'total_assessments': total_count,
        'avg_co2': round(total_co2 / total_count, 2) if total_count > 0 else 0,
        'avg_circ': round(total_circ / total_count, 2) if total_count > 0 else 0
    }

    return render_template('admin.html', 
                          metal_labels=metal_labels, 
                          metal_values=metal_values,
                          trend_labels=sorted_dates,
                          trend_values=avg_co2_per_day,
                          high_impact=aggregated_companies, # Reuse variable name for simplicity in template
                          stats=stats)


@app.route("/admin/notify_company", methods=['POST'])
def admin_notify_company():
    """Send an email notification to users of an unsustainable company."""
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'message': 'Admin access required.'}), 403

    data = request.get_json()
    company_name = data.get('company_name', '').strip()
    avg_co2 = data.get('avg_co2', 0)
    avg_circ = data.get('avg_circ', 0)
    month = data.get('month', '')

    if not company_name:
        return jsonify({'success': False, 'message': 'Company name is required.'}), 400

    # Find all registered users belonging to this company
    users = User.query.filter(
        db.func.lower(User.company_name) == db.func.lower(company_name)
    ).all()

    if not users:
        return jsonify({'success': False, 'message': f'No registered users found for company "{company_name}".'}), 404

    recipients = [u.email for u in users]
    sent_count = 0
    errors = []

    for user in users:
        try:
            msg = Message(
                subject=f"[LCA Platform] Sustainability Alert for {company_name}",
                sender=app.config['MAIL_USERNAME'],
                recipients=[user.email]
            )
            msg.html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; background: #0f172a; color: #e2e8f0; border-radius: 12px; overflow: hidden;">
                <div style="background: linear-gradient(135deg, #ef4444, #dc2626); padding: 32px; text-align: center;">
                    <h1 style="color: white; margin: 0; font-size: 24px;">⚠️ Sustainability Alert</h1>
                    <p style="color: rgba(255,255,255,0.85); margin: 8px 0 0;">Action Required — Environmental Performance Below Threshold</p>
                </div>
                <div style="padding: 32px;">
                    <p style="color: #94a3b8; font-size: 15px; line-height: 1.6;">Dear <strong style="color: #f1f5f9;">{user.username}</strong>,</p>
                    <p style="color: #94a3b8; font-size: 15px; line-height: 1.6;">
                        Our LCA monitoring platform has flagged <strong style="color: #f87171;">{company_name}</strong> as <strong style="color: #f87171;">Unsustainable</strong> based on the latest assessment data for <strong style="color: #f1f5f9;">{month}</strong>.
                    </p>
                    <div style="background: #1e293b; border: 1px solid rgba(239,68,68,0.3); border-radius: 10px; padding: 20px; margin: 24px 0;">
                        <h3 style="color: #f87171; margin: 0 0 12px; font-size: 14px; text-transform: uppercase; letter-spacing: 2px;">Current Performance Metrics</h3>
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr>
                                <td style="padding: 10px 0; color: #94a3b8; font-size: 14px;">Avg CO₂ Emissions</td>
                                <td style="padding: 10px 0; text-align: right; font-weight: bold; color: #f87171; font-size: 18px;">{avg_co2} <span style="font-size: 11px; color: #64748b;">kg/ton</span></td>
                            </tr>
                            <tr style="border-top: 1px solid rgba(255,255,255,0.05);">
                                <td style="padding: 10px 0; color: #94a3b8; font-size: 14px;">Avg Circularity Score</td>
                                <td style="padding: 10px 0; text-align: right; font-weight: bold; color: #f87171; font-size: 18px;">{avg_circ}<span style="font-size: 11px; color: #64748b;">%</span></td>
                            </tr>
                            <tr style="border-top: 1px solid rgba(255,255,255,0.05);">
                                <td style="padding: 10px 0; color: #94a3b8; font-size: 14px;">CO₂ Threshold</td>
                                <td style="padding: 10px 0; text-align: right; font-weight: bold; color: #10b981; font-size: 14px;">&lt; 1,000 kg/ton</td>
                            </tr>
                            <tr style="border-top: 1px solid rgba(255,255,255,0.05);">
                                <td style="padding: 10px 0; color: #94a3b8; font-size: 14px;">Circularity Threshold</td>
                                <td style="padding: 10px 0; text-align: right; font-weight: bold; color: #10b981; font-size: 14px;">&gt;= 50%</td>
                            </tr>
                        </table>
                    </div>
                    <div style="background: #1e293b; border-left: 3px solid #f59e0b; border-radius: 6px; padding: 16px; margin: 16px 0;">
                        <p style="margin: 0; color: #fbbf24; font-size: 13px; font-weight: bold;">⚡ Recommended Actions</p>
                        <ul style="color: #94a3b8; font-size: 13px; margin: 8px 0 0; padding-left: 18px; line-height: 1.8;">
                            <li>Switch to Secondary production routes to reduce CO₂ intensity.</li>
                            <li>Transition to Renewable energy sources for operations.</li>
                            <li>Increase recycled content and improve end-of-life recovery rates.</li>
                            <li>Optimize transport logistics to lower carbon footprint.</li>
                        </ul>
                    </div>
                    <p style="color: #64748b; font-size: 13px; line-height: 1.6; margin-top: 24px;">
                        Please log in to the LCA Platform to review your detailed assessment history and take corrective actions immediately.
                    </p>
                    <div style="text-align: center; margin: 28px 0 12px;">
                        <a href="http://localhost:5000" style="background: linear-gradient(135deg, #0ea5e9, #6366f1); color: white; padding: 14px 36px; border-radius: 50px; text-decoration: none; font-weight: bold; font-size: 14px; display: inline-block;">View Assessment Dashboard</a>
                    </div>
                </div>
                <div style="background: #0a0f1e; padding: 20px; text-align: center; border-top: 1px solid rgba(255,255,255,0.05);">
                    <p style="color: #334155; font-size: 11px; margin: 0;">This is an automated notification from the LCA Environmental Monitoring Platform. Please do not reply to this email.</p>
                </div>
            </div>
            """
            mail.send(msg)
            sent_count += 1
        except Exception as e:
            errors.append(str(e))

    if sent_count > 0:
        return jsonify({
            'success': True,
            'message': f'Notification sent to {sent_count} user(s) at {company_name}.',
            'sent_count': sent_count,
            'recipients': recipients
        })
    else:
        return jsonify({
            'success': False,
            'message': f'Failed to send notifications. Errors: {"; ".join(errors)}'
        }), 500


if __name__ == "__main__":
    socketio.run(app, debug=True)
