"""Evidence-backed draft history and explicit, revision-checked tree adoption."""
from copy import deepcopy

from app.domain.errors import DomainError
from app.domain import goal_drafts as rules
from app.domain.goals import Goal, GoalTree
from app.domain.models import User
from .common import new_id


class GoalDraftUseCases:
    @staticmethod
    def _draft_record(repository, draft_id):
        row = repository.get('goal_drafts', draft_id)
        if row is None:
            raise DomainError(404, 'Goal draft not found')
        return row

    def _draft_author(self, repository, actor, draft):
        user = User.from_record(self._user(repository, actor))
        rules.require_author(user, draft)
        return user

    @staticmethod
    def _draft_contexts(repository, ids):
        contexts = []
        for version_id in ids:
            version = repository.version_details(version_id)
            if version is None:
                raise DomainError(409, 'Goal draft source is missing; select current basis knowledge')
            space = repository.get('spaces', version['space_id'])
            if space is None:
                raise DomainError(409, 'Goal draft source space is missing')
            head = repository.document_active_head(version['document_id'])
            contexts.append({'version': version, 'space': space, 'membership': None,
                'current_version_id': head['id'] if head else None,
                'withdrawn': bool(repository.get('document_withdrawals', version['document_id'])),
                'deleted': bool(repository.get('version_lifecycle', version_id)),
                'space_deleted': bool(repository.get('space_deletions', space['id']))})
        return contexts

    def _draft_sources(self, repository, user, ids, *, expected=None):
        contexts = self._draft_contexts(repository, ids)
        for context in contexts:
            context['membership'] = repository.membership(context['space']['id'], user.id)
        sources = rules.validate_sources(user, contexts, expected=expected)
        return sources, [context['version'] for context in contexts]

    def _validated_draft(self, repository, user, draft):
        if draft['method'] not in (rules.METHOD, 'ai') or draft['sources_hash'] != rules.canonical_hash(draft['sources']):
            raise DomainError(409, 'Goal draft provenance is inconsistent')
        _, versions = self._draft_sources(repository, user,
            [source['version_id'] for source in draft['sources']], expected=draft['sources'])
        return versions

    def _draft_view(self, repository, user, row):
        """History is readable independently of present publication eligibility."""
        result, reasons = deepcopy(row), []
        try:
            versions = self._validated_draft(repository, user, row)
            tree = repository.list('goal_nodes')
            if rules.tree_hash(tree) != row['base_tree_hash']:
                reasons.append('Goal tree changed; generate fresh proposals before publication')
            # Normalize schema-six nodes in the detached view so legacy source
            # anchors receive the same field defaults as the publication path.
            normalized = rules.validate_nodes(row['nodes'], versions, tree)
            result['nodes'] = normalized
            rules.require_publishable_nodes(normalized)
        except DomainError as exc:
            reasons.append(str(exc))
        if row.get('abandoned_at') is not None:
            result['status'] = 'abandoned'
            reasons.append('This draft was abandoned; evidence is retained, not deleted')
        elif row['status'] != 'draft':
            reasons.append('This draft has already been published')
        if row.get('regenerates_id'):
            reasons.append('Regenerated differences require individual adoption, not whole-draft publication')
        result['publication_readiness'] = {'publishable': not reasons, 'reasons': reasons}
        if not result.get('generation'):
            result['generation'] = {'method': 'rule' if row['method'] == rules.METHOD else 'ai'}
        result.setdefault('suggestions', [])
        # Do not turn stored quotes into a bypass after moving the source private.
        for source in row['sources']:
            try:
                version = self._version(repository, user.id, source['version_id'])
                space = repository.get('spaces', version['space_id'])
                readable = space is not None and space.get('kind') == 'basis'
            except DomainError:
                readable = False
            if not readable:
                result['nodes'] = []
                result['suggestions'] = []
                # Retain identifiers/hashes for history, never withdrawn quotes.
                result['source_details_redacted'] = True
                break
        return result

    def _goal_generation_adapter(self):
        """Product paths are local-only, even if an adapter is accidentally injected."""
        if not getattr(self, '_allow_experimental_goal_generator', False):
            return None
        return getattr(self, '_goal_generator', None)

    def _generate_proposals(self, actor, ids):
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._owner(repository, actor))
            sources, versions = self._draft_sources(repository, user, ids)
        generator = self._goal_generation_adapter()
        if generator is None:
            output = {'nodes': rules.extract_nodes(versions), 'generation': {'method': 'rule'}}
        else:
            def check():
                with self._uow_factory() as check_uow:
                    current_user = User.from_record(self._owner(check_uow.repository, actor))
                    self._draft_sources(check_uow.repository, current_user, ids, expected=sources)
            output = generator.generate(deepcopy(versions), check=check)
        if not isinstance(output, dict) or not isinstance(output.get('generation'), dict):
            raise DomainError(502, 'Invalid goal generation result')
        method = output['generation'].get('method')
        if method not in ('rule', 'ai'):
            raise DomainError(502, 'Invalid goal generation method')
        for node in output.get('nodes', []):
            if not isinstance(node, dict):
                raise DomainError(422, 'Invalid goal generation node')
            node['generation_method'] = method
        return sources, versions, output

    def _insert_draft(self, repository, actor, sources, versions, output, *, previous=None):
        tree = repository.list('goal_nodes')
        proposed = deepcopy(output['nodes'])
        if previous:
            proposed = self._align_regenerated_nodes(repository, previous, proposed, sources)
        nodes = rules.validate_nodes(proposed, versions, tree)
        # The generated plan may never rely on invented or absent anchors.
        rules.require_publishable_nodes(nodes)
        now, draft_id = self._clock(), new_id()
        generation = deepcopy(output['generation'])
        limitations = list(rules.LIMITATIONS) if generation['method'] == 'rule' else [
            'AI-assisted proposals, not formal requirements; every populated field requires original evidence.',
            'Missing metrics, deadlines and constraints remain empty; owner confirmation is mandatory.']
        row = {'id': draft_id, 'created_by': actor, 'status': 'draft', 'revision': 1,
            'sources': sources, 'sources_hash': rules.canonical_hash(sources),
            'base_tree_hash': rules.tree_hash(tree), 'nodes': nodes,
            'method': rules.METHOD if generation['method'] == 'rule' else 'ai',
            'generation': generation, 'suggestions': [], 'regenerates_id': previous['id'] if previous else None,
            'limitations': limitations, 'created_at': now, 'updated_at': now, 'published_at': None, 'abandoned_at': None}
        if previous:
            row['suggestions'] = self._draft_differences(repository, previous, nodes)
        repository.add('goal_drafts', row)
        repository.add('goal_generations', {'draft_id': draft_id, 'snapshot': deepcopy(row), 'created_at': now})
        self._event(repository, actor, 'goal_draft_generated', 'goal_draft', draft_id)
        return self._draft_view(repository, User.from_record(self._owner(repository, actor)), row)

    def generate_goal_draft(self, actor: str, version_ids: list[str]) -> dict:
        ids = rules.version_ids(version_ids)
        sources, versions, output = self._generate_proposals(actor, ids)
        with self._uow_factory() as uow:
            user = User.from_record(self._owner(uow.repository, actor))
            self._draft_sources(uow.repository, user, ids, expected=sources)
            return self._insert_draft(uow.repository, actor, sources, versions, output)

    def prepare_goal_generation(self, actor, version_ids, *, regenerates_id=None, expected_revision=None):
        """Read a bounded immutable snapshot; also used to reauthorize live runs."""
        ids = rules.version_ids(version_ids)
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._owner(repository, actor))
            sources, versions = self._draft_sources(repository, user, ids)
            previous = None
            if regenerates_id is not None:
                previous = self._draft_record(repository, regenerates_id)
                self._draft_author(repository, actor, previous)
                if expected_revision is not None and (type(expected_revision) is not int or expected_revision != previous['revision']):
                    raise DomainError(409, 'Goal draft revision is stale')
                self._published_mapping(repository, previous)
            return {'sources': sources, 'versions': versions, 'previous': deepcopy(previous)}

    def check_goal_generation(self, actor, snapshot, *, authorization_check=None):
        with self._uow_factory() as uow:
            self._check_goal_generation(uow.repository, actor, snapshot, authorization_check)

    def _check_goal_generation(self, repository, actor, snapshot, authorization_check=None):
        if authorization_check is not None:
            authorization_check(repository)
        user = User.from_record(self._owner(repository, actor))
        self._draft_sources(repository, user, [s['version_id'] for s in snapshot['sources']], expected=snapshot['sources'])
        previous = snapshot['previous']
        if previous:
            current = self._draft_record(repository, previous['id'])
            self._draft_author(repository, actor, current)
            if current['revision'] != previous['revision']:
                raise DomainError(409, 'Goal draft changed during regeneration')
            self._published_mapping(repository, current)

    def execute_goal_generation(self, actor, snapshot, *, emit, check, commit_guard, authorization_check=None):
        """Actual synchronous stages, with cancellation linearized at durable COMMIT."""
        from app.domain.ai_ports import answer_commit_guard

        def stage(kind, **data):
            check()
            emit({'type': kind, **data})

        stage('sources.validation.started')
        self.check_goal_generation(actor, snapshot, authorization_check=authorization_check)
        stage('sources.validation.completed', source_count=len(snapshot['sources']))
        stage('sources.read.started')
        # Reload the authorized text, do not trust a stale caller-provided body.
        current = self.prepare_goal_generation(actor, [s['version_id'] for s in snapshot['sources']],
            regenerates_id=snapshot['previous']['id'] if snapshot['previous'] else None,
            expected_revision=snapshot['previous']['revision'] if snapshot['previous'] else None)
        if current['sources'] != snapshot['sources']:
            raise DomainError(409, 'Goal draft sources changed')
        versions = current['versions']
        stage('sources.read.completed', source_count=len(versions), character_count=sum(len(v['content']) for v in versions))
        generator = self._goal_generation_adapter()
        stage('generation.started', method='ai' if generator else 'rule')
        output = generator.generate(deepcopy(versions), check=check) if generator else {
            'nodes': rules.extract_nodes(versions), 'generation': {'method': 'rule'}}
        stage('generation.completed')
        stage('evidence.validation.started')
        if (not isinstance(output, dict) or not isinstance(output.get('generation'), dict)
                or output['generation'].get('method') not in ('rule', 'ai') or not isinstance(output.get('nodes'), list)):
            raise DomainError(502, 'Invalid goal generation result')
        for node in output['nodes']:
            if not isinstance(node, dict):
                raise DomainError(422, 'Invalid goal generation node')
            node['generation_method'] = output['generation']['method']
        with self._uow_factory() as uow:
            self._check_goal_generation(uow.repository, actor, snapshot, authorization_check)
            nodes = rules.validate_nodes(output['nodes'], versions, uow.repository.list('goal_nodes'))
            rules.require_publishable_nodes(nodes)
        stage('evidence.validation.completed', node_count=len(nodes))
        stage('draft.save.started')
        token = answer_commit_guard.set(commit_guard)
        try:
            with self._uow_factory() as uow:
                def validate():
                    self._check_goal_generation(uow.repository, actor, snapshot, authorization_check)
                validate()
                commit_guard.check()
                commit_guard.validate = validate
                draft = self._insert_draft(uow.repository, actor, snapshot['sources'], versions, output,
                                           previous=snapshot['previous'])
                commit_guard.result = {'draft': deepcopy(draft)}
            # No yielding/check after COMMIT: a durable draft wins cancellation.
            return {'draft': draft}
        finally:
            commit_guard.validate = None
            answer_commit_guard.reset(token)

    def list_goal_drafts(self, actor: str) -> list[dict]:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._owner(repository, actor))
            return [self._draft_view(repository, user, row)
                    for row in repository.list('goal_drafts', created_by=actor)]

    def get_goal_draft(self, actor: str, draft_id: str) -> dict:
        with self._uow_factory() as uow:
            row = self._draft_record(uow.repository, draft_id)
            user = self._draft_author(uow.repository, actor, row)
            return self._draft_view(uow.repository, user, row)

    def update_goal_draft(self, actor, draft_id, expected_revision, nodes):
        with self._uow_factory() as uow:
            repository = uow.repository
            row = self._draft_record(repository, draft_id)
            user = self._draft_author(repository, actor, row)
            rules.require_revision(row, expected_revision)
            if row.get('regenerates_id'):
                raise DomainError(409, 'Difference proposals are immutable; regenerate instead')
            versions = self._validated_draft(repository, user, row)
            tree = repository.list('goal_nodes')
            if rules.tree_hash(tree) != row['base_tree_hash']:
                raise DomainError(409, 'Goal tree changed; generate a new draft')
            validated = rules.validate_nodes(nodes, versions, tree, previous=row['nodes'])
            changes = {'nodes': validated, 'revision': row['revision'] + 1, 'updated_at': self._clock()}
            repository.update('goal_drafts', draft_id, changes)
            self._event(repository, actor, 'goal_draft_updated', 'goal_draft', draft_id)
            return self._draft_view(repository, user, {**row, **changes})

    def _generation_receipt(self, repository, actor, draft, node, goal_id, operation, *, goal_revision=None):
        now = self._clock()
        repository.add('goal_generation_sources', {'id': new_id(), 'goal_id': goal_id,
            'draft_id': draft['id'], 'node_id': node['id'], 'node': deepcopy(node),
            'generation': draft.get('generation') or {'method': 'rule'}, 'operation': operation,
            'confirmation': {'actor_id': actor, 'confirmed': True, 'draft_revision': draft['revision'],
                'goal_revision': goal_revision, 'confirmed_at': now, 'comment': '',
                'nodes_hash': rules.canonical_hash(draft['nodes']), 'node_hash': rules.canonical_hash(node),
                'sources_hash': draft['sources_hash']}, 'created_at': now})

    def _insert_generated_goal(self, repository, actor, node, goal_id, parent):
        now = self._clock()
        repository.add('goal_nodes', {'id': goal_id, 'parent_id': parent,
            **{field: node.get(field) for field in rules.FIELDS}, 'position': node['position'],
            'status': 'not_started', 'revision': 1, 'created_by': actor,
            'created_at': now, 'updated_at': now, 'archived_at': None, 'archived_reason': ''})
        version_ids = {c['version_id'] for values in node.get('field_sources', {}).values() for c in values}
        if node.get('source'):
            version_ids.add(node['source']['version_id'])
        for version_id in version_ids:
            repository.add('goal_knowledge', {'id': new_id(), 'goal_id': goal_id,
                'version_id': version_id, 'linked_by': actor, 'created_at': now})

    def publish_goal_draft(self, actor, draft_id, expected_revision, *, confirmed=False):
        rules.require_confirmation(confirmed)
        with self._uow_factory() as uow:
            repository = uow.repository
            row = self._draft_record(repository, draft_id)
            user = self._draft_author(repository, actor, row)
            publication = repository.get('goal_draft_publications', draft_id)
            if publication is not None:
                return publication['result']
            rules.require_revision(row, expected_revision)
            if row.get('regenerates_id'):
                raise DomainError(409, 'Adopt regenerated differences individually')
            versions = self._validated_draft(repository, user, row)
            tree = repository.list('goal_nodes')
            if rules.tree_hash(tree) != row['base_tree_hash']:
                raise DomainError(409, 'Goal tree changed; generate a new draft before publication')
            nodes = rules.validate_nodes(row['nodes'], versions, tree)
            rules.require_publishable_nodes(nodes)
            mapping, now = {node['id']: new_id() for node in nodes}, self._clock()
            for node in nodes:
                goal_id = mapping[node['id']]
                self._insert_generated_goal(repository, actor, node, goal_id, mapping.get(node['parent_id'], node['parent_id']))
                self._generation_receipt(repository, actor, row, node, goal_id, 'add')
            result = {'draft_id': draft_id, 'goal_ids': list(mapping.values()), 'node_id_mapping': mapping, 'published_at': now}
            repository.add('goal_draft_publications', {'draft_id': draft_id, 'result': result, 'created_at': now})
            repository.update('goal_drafts', draft_id, {'status': 'published', 'revision': row['revision'] + 1,
                'updated_at': now, 'published_at': now})
            self._event(repository, actor, 'goal_draft_published', 'goal_draft', draft_id)
            return result

    @staticmethod
    def _published_mapping(repository, draft, _seen=None):
        seen = set() if _seen is None else _seen
        if draft['id'] in seen or len(seen) >= 64:
            raise DomainError(409, 'Regeneration history is cyclic or exceeds 64 generations')
        seen.add(draft['id'])
        publication = repository.get('goal_draft_publications', draft['id'])
        if publication:
            return publication['result']['node_id_mapping']
        if draft.get('regenerates_id'):
            mapping = GoalDraftUseCases._published_mapping(repository,
                GoalDraftUseCases._draft_record(repository, draft['regenerates_id']), seen)
            for suggestion in draft.get('suggestions', []):
                if suggestion['status'] == 'adopted':
                    if suggestion['operation'] == 'retire':
                        mapping.pop(suggestion['node']['id'], None)
                    else:
                        mapping[suggestion['node']['id']] = suggestion['result_goal_id']
            return mapping
        raise DomainError(409, 'Regeneration requires a published mapping')

    @staticmethod
    def _align_regenerated_nodes(repository, previous, nodes, sources):
        """Match published source identity, never temporary list indices alone."""
        old_documents = {s['version_id']: s['document_id'] for s in previous['sources']}
        new_documents = {s['version_id']: s['document_id'] for s in sources}
        unmatched = {n['id']: n for n in previous['nodes']}
        mapping = {}
        for node in nodes:
            citation = node.get('source') or {}
            document = new_documents.get(citation.get('version_id'))
            candidates = [old for old in unmatched.values()
                if old_documents.get((old.get('source') or {}).get('version_id')) == document]
            exact = [old for old in candidates if old['title'] == node['title']]
            positional = [old for old in candidates if (old.get('source') or {}).get('start') == citation.get('start')]
            match = exact[0] if len(exact) == 1 else positional[0] if len(positional) == 1 else None
            new_id_value = match['id'] if match else 'regenerated-' + new_id()
            mapping[node['id']] = new_id_value
            if match:
                unmatched.pop(match['id'])
        for node in nodes:
            node['parent_id'] = mapping.get(node['parent_id'], node['parent_id'])
            node['id'] = mapping[node['id']]
        return nodes

    def _draft_differences(self, repository, previous, nodes):
        mapping = self._published_mapping(repository, previous)
        old = {n['id']: n for n in previous['nodes']}
        new = {n['id']: n for n in nodes}
        suggestions = []
        def add(operation, node, goal=None):
            suggestions.append({'id': new_id(), 'operation': operation, 'node': deepcopy(node),
                'target_goal_id': goal['id'] if goal else None,
                'expected_goal_revision': goal['revision'] if goal else None, 'before': deepcopy(goal), 'status': 'pending'})
        for node in nodes:
            goal = repository.get('goal_nodes', mapping[node['id']]) if node['id'] in mapping else None
            if goal is None or goal['archived_at'] is not None:
                add('add', node)
                continue
            if any(goal.get(field) != node.get(field) for field in rules.FIELDS):
                add('modify', node, goal)
            parent = mapping.get(node['parent_id'], node['parent_id'])
            if goal['parent_id'] != parent or goal['position'] != node['position']:
                add('move', node, goal)
        for node_id, goal_id in mapping.items():
            goal = repository.get('goal_nodes', goal_id)
            if node_id not in new and goal and goal['archived_at'] is None and node_id in old:
                add('retire', old[node_id], goal)
        return suggestions

    def regenerate_goal_draft(self, actor, draft_id, version_ids, expected_revision=None):
        ids = rules.version_ids(version_ids)
        with self._uow_factory() as uow:
            previous = self._draft_record(uow.repository, draft_id)
            self._draft_author(uow.repository, actor, previous)
            if expected_revision is not None and (type(expected_revision) is not int or expected_revision != previous['revision']):
                raise DomainError(409, 'Goal draft revision is stale')
            self._published_mapping(uow.repository, previous)
            revision = previous['revision']
        sources, versions, output = self._generate_proposals(actor, ids)
        with self._uow_factory() as uow:
            current = self._draft_record(uow.repository, draft_id)
            user = self._draft_author(uow.repository, actor, current)
            if current['revision'] != revision:
                raise DomainError(409, 'Goal draft changed during regeneration')
            self._draft_sources(uow.repository, user, ids, expected=sources)
            return self._insert_draft(uow.repository, actor, sources, versions, output, previous=current)

    def abandon_goal_draft(self, actor, draft_id, expected_revision, *, confirmed=False):
        rules.require_confirmation(confirmed)
        with self._uow_factory() as uow:
            row = self._draft_record(uow.repository, draft_id)
            user = self._draft_author(uow.repository, actor, row)
            rules.require_revision(row, expected_revision)
            changes = {'abandoned_at': self._clock(), 'revision': row['revision'] + 1, 'updated_at': self._clock()}
            uow.repository.update('goal_drafts', draft_id, changes)
            self._event(uow.repository, actor, 'goal_draft_abandoned', 'goal_draft', draft_id)
            return self._draft_view(uow.repository, user, {**row, **changes})

    def reject_goal_draft_suggestion(self, actor, draft_id, suggestion_id, expected_revision, *, reason='', confirmed=False):
        from app.domain.policies import text
        rules.require_confirmation(confirmed)
        text(reason, 'suggestion rejection reason', 4000, empty=True)
        with self._uow_factory() as uow:
            row = self._draft_record(uow.repository, draft_id)
            user = self._draft_author(uow.repository, actor, row)
            rules.require_revision(row, expected_revision)
            suggestions = deepcopy(row.get('suggestions', []))
            suggestion = next((s for s in suggestions if s['id'] == suggestion_id), None)
            if suggestion is None:
                raise DomainError(404, 'Goal difference suggestion not found')
            if suggestion['status'] != 'pending':
                raise DomainError(409, 'Goal difference was already decided')
            suggestion.update(status='rejected', reason=reason, decided_by=actor, decided_at=self._clock())
            changes = {'suggestions': suggestions, 'revision': row['revision'] + 1, 'updated_at': self._clock()}
            uow.repository.update('goal_drafts', draft_id, changes)
            self._event(uow.repository, actor, 'goal_draft_suggestion_rejected', 'goal_draft', draft_id)
            return self._draft_view(uow.repository, user, {**row, **changes})

    def adopt_goal_draft_suggestion(self, actor, draft_id, suggestion_id, expected_revision,
                                    *, expected_goal_revision=None, confirmed=False):
        rules.require_confirmation(confirmed)
        with self._uow_factory() as uow:
            repository = uow.repository
            draft = self._draft_record(repository, draft_id)
            user = self._draft_author(repository, actor, draft)
            rules.require_revision(draft, expected_revision)
            if not draft.get('regenerates_id'):
                raise DomainError(409, 'This draft has no regenerated differences')
            suggestions = deepcopy(draft['suggestions'])
            proposal = next((s for s in suggestions if s['id'] == suggestion_id), None)
            if proposal is None:
                raise DomainError(404, 'Goal difference suggestion not found')
            if proposal['status'] != 'pending':
                raise DomainError(409, 'This suggestion was already adopted')
            versions = self._validated_draft(repository, user, draft)
            node, operation = proposal['node'], proposal['operation']
            mapping = self._published_mapping(repository, draft)
            parent = mapping.get(node['parent_id'], node['parent_id'])
            tree = GoalTree(repository.list('goal_nodes'))
            now = self._clock()
            if operation == 'add':
                # Validate only the proposed addition, with parent resolved to a
                # published or already-adopted goal; dependencies stay explicit.
                candidate = {**node, 'parent_id': parent}
                rules.validate_nodes([candidate], versions, repository.list('goal_nodes'))
                rules.require_publishable_nodes([candidate])
                tree.require_placement(None, parent)
                goal_id = new_id()
                self._insert_generated_goal(repository, actor, node, goal_id, parent)
                revision = None
            else:
                goal_id = proposal['target_goal_id']
                goal = self._goal_record(repository, goal_id)
                if expected_goal_revision != proposal['expected_goal_revision']:
                    raise DomainError(409, 'Explicit expected goal revision must match the proposal baseline')
                Goal.from_record(goal).require_management(user, expected_goal_revision)
                revision = goal['revision']
                changes = {'revision': revision + 1, 'updated_at': now}
                if operation == 'retire':
                    Goal.from_record(goal).require_archive(repository.list('goal_nodes', parent_id=goal_id))
                    changes.update(archived_at=now, archived_reason='Owner-confirmed regenerated retirement suggestion')
                elif operation == 'move':
                    tree.require_placement(goal_id, parent)
                    changes.update(parent_id=parent, position=node['position'])
                elif operation == 'modify':
                    rules.require_publishable_nodes([node])
                    for citations in node.get('field_sources', {}).values():
                        for citation in citations:
                            rules.validate_anchor(citation, {v['id']: v for v in versions})
                    changes.update({field: node.get(field) for field in rules.FIELDS})
                else:
                    raise DomainError(422, 'Unknown goal difference operation')
                repository.update('goal_nodes', goal_id, changes)
            self._generation_receipt(repository, actor, draft, node, goal_id, operation, goal_revision=revision)
            proposal.update(status='adopted', result_goal_id=goal_id)
            # A second suggestion for this goal must use the newly committed
            # revision, but only after this explicit adoption advanced it.
            for other in suggestions:
                if other['status'] == 'pending' and other['target_goal_id'] == goal_id:
                    other['expected_goal_revision'] = (revision or 0) + 1
            changes = {'suggestions': suggestions, 'revision': draft['revision'] + 1, 'updated_at': now}
            repository.update('goal_drafts', draft_id, changes)
            self._event(repository, actor, 'goal_draft_suggestion_adopted', 'goal_draft', draft_id)
            return self._draft_view(repository, user, {**draft, **changes})
