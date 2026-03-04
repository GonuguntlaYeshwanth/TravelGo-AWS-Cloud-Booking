from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_cors import CORS
import uuid
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

app = Flask(__name__)
CORS(app)
app.secret_key = "travelgo_secret_key"


# ----------------------------
# AWS CONFIG
# ----------------------------
REGION = "us-west-1"
SNS_TOPIC_ARN = "arn:aws:sns:us-west-1:743664054042:travelgo-notify"


# ----------------------------
# DynamoDB
# ----------------------------
dynamodb = boto3.resource("dynamodb", region_name=REGION)

users_table = dynamodb.Table("travel_users")
bookings_table = dynamodb.Table("travel_bookings")


# ----------------------------
# SNS CLIENT
# ----------------------------
sns_client = boto3.client("sns", region_name=REGION)


def send_notification(subject, message):
    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message
        )
    except ClientError as e:
        app.logger.error(f"SNS publish failed: {e}")


# ----------------------------
# In-Memory Seat Lock
# ----------------------------
bus_seat_map = {1: []}


# ============================
# HOME
# ============================
@app.route("/")
def index():
    return render_template("index.html")


# ============================
# REGISTER
# ============================
@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form["name"]
        email = request.form["email"]
        password = request.form["password"]

        existing = users_table.get_item(Key={"email": email}).get("Item")

        if existing:
            return "User already exists!", 400

        users_table.put_item(
            Item={
                "email": email,
                "name": name,
                "password": password
            }
        )

        return redirect(url_for("login"))

    return render_template("register.html")


# ============================
# LOGIN
# ============================
@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"]
        password = request.form["password"]

        user = users_table.get_item(Key={"email": email}).get("Item")

        if user and user["password"] == password:
            session["email"] = email
            return redirect(url_for("dashboard"))

        return "Invalid Credentials", 401

    return render_template("login.html")


# ============================
# LOGOUT
# ============================
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ============================
# DASHBOARD
# ============================
@app.route("/dashboard")
def dashboard():

    if "email" not in session:
        return redirect(url_for("login"))

    all_items = bookings_table.scan().get("Items", [])

    user_bookings = [
        b for b in all_items if b.get("email") == session["email"]
    ]

    return render_template("dashboard.html", bookings=user_bookings)


# ============================
# UPDATE BOOKING
# ============================
@app.route("/update_booking/<booking_id>", methods=["POST"])
def update_booking(booking_id):

    if "email" not in session:
        return redirect(url_for("login"))

    source = request.form["source"]
    destination = request.form["destination"]
    date = request.form["date"]
    status = request.form["status"]

    bookings_table.update_item(
        Key={"booking_id": booking_id},
        UpdateExpression="SET #src=:s, destination=:d, #dt=:dt, #st=:st",
        ExpressionAttributeNames={
            "#src": "source",
            "#dt": "date",
            "#st": "status"
        },
        ExpressionAttributeValues={
            ":s": source,
            ":d": destination,
            ":dt": date,
            ":st": status
        }
    )

    send_notification(
        "TravelGo Booking Updated",
        f"Booking {booking_id} updated\nFrom: {source}\nTo: {destination}\nDate: {date}\nStatus: {status}"
    )

    return redirect(url_for("dashboard"))


# ============================
# DELETE BOOKING
# ============================
@app.route("/delete_booking/<booking_id>", methods=["POST"])
def delete_booking(booking_id):

    if "email" not in session:
        return redirect(url_for("login"))

    bookings_table.delete_item(Key={"booking_id": booking_id})

    send_notification(
        "TravelGo Booking Deleted",
        f"Booking {booking_id} has been deleted."
    )

    return redirect(url_for("dashboard"))


# ============================
# BUS SEARCH
# ============================
@app.route("/bus", methods=["GET", "POST"])
def bus():

    if "email" not in session:
        return redirect(url_for("login"))

    buses = []

    if request.method == "POST":

        source = request.form["source"]
        destination = request.form["destination"]
        date = request.form["date"]

        buses = [{
            "id": 1,
            "name": "Orange Travels",
            "source": source,
            "destination": destination,
            "date": date,
            "price": 1200
        }]

    return render_template("bus.html", buses=buses)


# ============================
# SEAT SELECTION
# ============================
@app.route("/select_seats/<int:bus_id>", methods=["GET", "POST"])
def select_seats(bus_id):

    if "email" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        seats = request.form.getlist("seats")

        session["pending_booking"] = {
            "type": "Bus",
            "source": "Hyderabad",
            "destination": "Bangalore",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "price": len(seats) * 1200,
            "seats": seats,
            "bus_id": bus_id
        }

        return redirect(url_for("payment"))

    booked = bus_seat_map.get(bus_id, [])

    return render_template(
        "select_seats.html",
        bus_id=bus_id,
        booked_seats=booked
    )


# ============================
# PAYMENT
# ============================
@app.route("/payment", methods=["GET", "POST"])
def payment():

    if "email" not in session:
        return redirect(url_for("login"))

    booking = session.get("pending_booking")

    if not booking:
        return redirect(url_for("dashboard"))

    if request.method == "POST":

        if booking["type"] == "Bus":

            bus_id = booking["bus_id"]
            seats = booking.get("seats", [])

            already = bus_seat_map.get(bus_id, [])

            conflict = [s for s in seats if s in already]

            if conflict:
                return f"Seats already booked: {','.join(conflict)}", 409

            bus_seat_map.setdefault(bus_id, []).extend(seats)

        booking_id = str(uuid.uuid4())

        bookings_table.put_item(
            Item={
                "booking_id": booking_id,
                "email": session["email"],
                "type": booking["type"],
                "source": booking["source"],
                "destination": booking["destination"],
                "date": booking["date"],
                "price": booking["price"],
                "seats": booking.get("seats", []),
                "status": "Confirmed",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        )

        send_notification(
            "TravelGo Booking Confirmed",
            f"Booking Confirmed\nService:{booking['type']}\nFrom:{booking['source']}\nTo:{booking['destination']}\nPrice:{booking['price']}"
        )

        session.pop("pending_booking", None)

        return redirect(url_for("dashboard"))

    return render_template("payment.html", booking=booking)


# ============================
# API BOOK ENDPOINT
# ============================
@app.route("/book", methods=["POST"])
def book_trip():

    data = request.get_json()

    if not data:
        return jsonify({"error": "JSON body required"}), 400

    booking_id = data.get("booking_id") or str(uuid.uuid4())
    email = data.get("email", "unknown@travelgo.com")
    destination = data.get("destination", "Unknown")
    source = data.get("source", "Unknown")
    btype = data.get("type", "General")
    date = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    price = data.get("price", 0)

    bookings_table.put_item(
        Item={
            "booking_id": booking_id,
            "email": email,
            "type": btype,
            "source": source,
            "destination": destination,
            "date": date,
            "price": price,
            "status": "Confirmed",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    )

    send_notification(
        "TravelGo API Booking Confirmed",
        f"Booking {booking_id} confirmed for {destination}"
    )

    return jsonify({
        "status": "success",
        "booking_id": booking_id,
        "destination": destination
    }), 201


# ============================
# RUN SERVER
# ============================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)