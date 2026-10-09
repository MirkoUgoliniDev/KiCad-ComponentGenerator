"""Grid-safe batch transforms; callers validate all pins before accepting a batch."""
from copy import deepcopy
from .symbol import PIN_GRID_MM, layout_symbol, symbol_inputs


def transform_selection(rows, numbers, primary, operation, values=()):
    result = deepcopy(rows)
    chosen = [r for r in result if str(r['pin']) in numbers]
    if not chosen:
        return result
    anchor = next((r for r in chosen if str(r['pin']) == primary), chosen[0])
    ax, ay = anchor['symbol_position']
    if operation == 'move':
        dx, dy = [round(v / PIN_GRID_MM) * PIN_GRID_MM for v in values]
        for row in chosen:
            x, y = row['symbol_position']
            row['symbol_position'] = [round(x + dx, 4), round(y + dy, 4)]
    elif operation == 'rotate':
        degrees = values[0]
        if degrees % 90:
            raise ValueError('Rotation must be a multiple of 90 degrees')
        for row in chosen:
            x, y = row['symbol_position']
            dx, dy = x - ax, y - ay
            for _ in range((degrees // 90) % 4):
                dx, dy = -dy, dx
            row['symbol_position'] = [round(ax + dx, 4), round(ay + dy, 4)]
            row['symbol_angle'] = (row['symbol_angle'] + degrees) % 360
    elif operation in ('align_x', 'align_y'):
        axis = 0 if operation == 'align_x' else 1
        for row in chosen:
            row['symbol_position'][axis] = anchor['symbol_position'][axis]
    elif operation in ('distribute_x', 'distribute_y'):
        if len(chosen) < 3:
            raise ValueError('At least three pins are required')
        axis = 0 if operation == 'distribute_x' else 1
        chosen.sort(key=lambda r: r['symbol_position'][axis])
        start, end = chosen[0]['symbol_position'][axis], chosen[-1]['symbol_position'][axis]
        step = max(1, round((end - start) / PIN_GRID_MM / (len(chosen) - 1))) * PIN_GRID_MM
        for index, row in enumerate(chosen):
            row['symbol_position'][axis] = round(start + index * step, 4)
    else:
        raise ValueError('Unknown transform')
    pins, body = symbol_inputs(result)
    layout_symbol(pins, body)
    return result
