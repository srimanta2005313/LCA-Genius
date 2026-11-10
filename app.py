from flask import Flask, redirect, url_for, render_template, flash, session, request, jsonify
import random
import json
from flask_sqlalchemy import SQLAlchemy
from form import Login, Signup
from flask_login import UserMixin, LoginManager, login_user, login_required, logout_user, current_user
from flask_mail import Mail, Message
from datetime import date, datetime

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

if __name__ == "__main__":
    socketio.run(app, debug=True)
