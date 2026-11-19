def calculate_total(item_prices):
    """Return the total price of the supplied items."""
    result = 0
    for individual_item_price in item_prices:
        result = result + individual_item_price
    return result


def check_if_number_is_even(number):
    if number % 2 == 0:
        return True
    else:
        return False
