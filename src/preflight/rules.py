import json
import yaml


class RuleEngine:
    def __init__(self):
        self.rules = []

    def load_json(self, path):
        with open(path, encoding='utf-8') as f:
            self.rules = json.load(f)

    def load_yaml(self, path):
        with open(path, encoding='utf-8') as f:
            self.rules = yaml.safe_load(f)

    def set_rules(self, rules):
        self.rules = rules

    def check(self, page_data):
        results = []
        if not isinstance(self.rules, list):
            return results

        for rule in self.rules:
            name = rule.get('name', 'Unknown')
            if name == 'Total Ink Coverage':
                max_tac = rule.get('max', 300)
                if page_data.get('tac_max', 0) > max_tac:
                    results.append({
                        'rule': name,
                        'status': 'FAIL',
                        'message': f"TAC {page_data['tac_max']:.0f}% > {max_tac}%"
                    })
            elif name == 'Spot Color Check':
                allowed = rule.get('allowed', [])
                spots = page_data.get('spots', [])
                for s in spots:
                    if s not in allowed:
                        results.append({
                            'rule': name,
                            'status': 'FAIL',
                            'message': f"Spot color '{s}' not allowed"
                        })
            elif name == 'Overprint Warning':
                if page_data.get('has_overprint'):
                    results.append({
                        'rule': name,
                        'status': 'WARN',
                        'message': "Page contains overprint objects"
                    })
            elif name == 'Trim Box Inside Media Box':
                tb = page_data.get('trim_box')
                mb = page_data.get('media_box')
                if tb and mb:
                    if not (tb.x0 >= mb.x0 and tb.y0 >= mb.y0
                            and tb.x1 <= mb.x1 and tb.y1 <= mb.y1):
                        results.append({
                            'rule': name,
                            'status': 'FAIL',
                            'message': "TrimBox is not inside MediaBox"
                        })
        return results
