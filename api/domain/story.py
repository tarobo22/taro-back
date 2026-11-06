"""Pure, versioned story graph and command rules. No storage or provider access."""
from copy import deepcopy
import re
from uuid import UUID


class StoryInvalid(ValueError): pass
class StoryConflict(Exception): pass
class StoryNotFound(LookupError): pass


def story_uuid(value):
    try: return UUID(str(value))
    except (ValueError, TypeError, AttributeError): raise StoryInvalid('invalid_identity') from None


def positive_version(value):
    if type(value) is not int or value < 1: raise StoryInvalid('invalid_version')
    return value


def strict_body(values, fields):
    if not isinstance(values, dict) or set(values) != set(fields): raise StoryInvalid('invalid_fields')


class StoryCatalog:
    def __init__(self, definition, episodes): self._definition, self._episodes = definition, episodes

    @classmethod
    def from_mapping(cls, definition):
        try:
            if set(definition) != {'episodes'} or not isinstance(definition['episodes'], list) or not definition['episodes']: raise ValueError()
            episodes = {}
            for raw in definition['episodes']:
                if set(raw) != {'id', 'version', 'title', 'summary', 'start_node', 'nodes'}: raise ValueError()
                if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', raw['id']) or not re.fullmatch(r'\d+\.\d+\.\d+', raw['version']): raise ValueError()
                if not all(isinstance(raw[k], str) and raw[k].strip() for k in ('title', 'summary', 'start_node')): raise ValueError()
                key = raw['id'], raw['version']
                if key in episodes or not isinstance(raw['nodes'], list): raise ValueError()
                nodes = {}
                for node in raw['nodes']:
                    fields = {'id', 'kind', 'prompt', 'expression', 'yes', 'no'} if node['kind'] == 'choice' else {'id', 'kind', 'title', 'body', 'action'}
                    if node['kind'] not in ('choice', 'ending') or set(node) != fields or node['id'] in nodes: raise ValueError()
                    if not all(isinstance(v, str) and v.strip() and len(v) <= 2000 for v in node.values()): raise ValueError()
                    if node['kind'] == 'choice' and node['expression'] not in ('calm', 'welcome', 'listen', 'encourage', 'joy', 'concern', 'serious'): raise ValueError()
                    nodes[node['id']] = deepcopy(node)
                visited, lengths = set(), []
                def walk(node_id, path, depth):
                    if node_id not in nodes or node_id in path: raise ValueError()
                    node = nodes[node_id]; visited.add(node_id)
                    if node['kind'] == 'ending':
                        if not 3 <= depth <= 5: raise ValueError()
                        lengths.append(depth)
                    else:
                        if depth >= 5: raise ValueError()
                        for next_id in (node['yes'], node['no']): walk(next_id, path | {node_id}, depth + 1)
                walk(raw['start_node'], set(), 0)
                if visited != set(nodes) or len({n['id'] for n in nodes.values() if n['kind'] == 'ending'}) < 2: raise ValueError()
                episodes[key] = {**deepcopy(raw), 'nodes': nodes, 'min_choices': min(lengths), 'max_choices': max(lengths)}
            return cls(deepcopy(definition), episodes)
        except (ValueError, TypeError, KeyError, AttributeError): raise StoryInvalid('invalid_episode_graph') from None

    def export_definition(self): return deepcopy(self._definition)

    def episode(self, episode_id, version=None):
        options = [e for (identity, _), e in self._episodes.items() if identity == episode_id and (version is None or e['version'] == version)]
        if not options: raise StoryNotFound('episode_not_found')
        return deepcopy(max(options, key=lambda e: tuple(map(int, e['version'].split('.')))))

    def summaries(self):
        return [{k: e[k] for k in ('id', 'version', 'title', 'summary', 'min_choices', 'max_choices')}
                for e in (self.episode(identity) for identity in dict.fromkeys(k[0] for k in self._episodes))]


def run_view(row, catalog):
    episode = catalog.episode(row['episode_id'], row['episode_version'])
    node = episode['nodes'].get(row['current_node_id']) if row['current_node_id'] else None
    ending = episode['nodes'].get(row['ending_id']) if row['ending_id'] else None
    return {'id': str(row['id']), 'episode_id': row['episode_id'], 'episode_version': row['episode_version'],
            'title': episode['title'], 'state': row['state'], 'version': row['version'],
            'current_node': {k: node[k] for k in ('id', 'prompt', 'expression')} if node else None,
            'ending': {k: ending[k] for k in ('id', 'title', 'body', 'action')} if ending else None,
            'choice_count': len(row['history']), 'created_at': row['created_at'].isoformat(),
            'updated_at': row['updated_at'].isoformat(),
            'completed_at': row['completed_at'].isoformat() if row['completed_at'] else None}
