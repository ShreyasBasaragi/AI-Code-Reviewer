def calculate_average(numbers):
    total = sum(numbers)
    return total / len(numbers)


def authenticate(username, password):
    if username == "admin" and password == "admin123":
        return True
    return False
