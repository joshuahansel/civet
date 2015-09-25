from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse, Http404, HttpResponseNotAllowed, HttpResponseForbidden
from django.core.urlresolvers import reverse
from django.core.exceptions import PermissionDenied
from django.conf import settings
from ci import models, event
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.contrib import messages
from datetime import timedelta
import time, os
from django.contrib.humanize.templatetags.humanize import naturaltime

import logging, traceback
logger = logging.getLogger('ci')

def sortable_time_str(d):
  return d.strftime('%Y%m%d%H%M%S')

def display_time_str(d):
  #return d.strftime('%H:%M:%S %m/%d/%y')
  return naturaltime(d)

def get_repos_status(last_modified=None):
  """
  Get a list of open PRs, sorted by repository.
  """
  repos = models.Repository.objects.order_by('name')
  if not repos:
    return []

  repos_data = []
  for repo in repos.all():
    branches = []
    q = repo.branches.exclude(status=models.JobStatus.NOT_STARTED)
    if last_modified:
      q = q.filter(last_modified__gte=last_modified)
    q = q.order_by('name')
    for branch in q.all():
      branches.append({'id': branch.pk,
        'name': branch.name,
        'status': branch.status_slug(),
        'url': reverse('ci:view_branch', args=[branch.pk,]),
        'last_modified_date': sortable_time_str(branch.last_modified),
        })

    prs = []
    q = repo.pull_requests.filter(closed=False)
    if last_modified:
      q = q.filter(last_modified__gte=last_modified)
    q = q.order_by('number')
    for pr in q.all():
      prs.append({'id': pr.pk,
        'title': pr.title,
        'number': pr.number,
        'status': pr.status_slug(),
        'user': pr.events.first().head.user().name,
        'url': reverse('ci:view_pr', args=[pr.pk,]),
        'last_modified_date': sortable_time_str(pr.last_modified),
        })

    if prs or branches:
      repos_data.append({'id': repo.pk,
        'name': repo.name,
        'branches': branches,
        'prs': prs,
        'url': reverse('ci:view_repo', args=[repo.pk,]),
        })

  return repos_data

def get_job_info(jobs, num):
  ret = []
  for job in jobs.order_by('-last_modified')[:num]:
    if job.event.pull_request:
      trigger = str(job.event.pull_request)
      trigger_url = reverse('ci:view_pr', args=[job.event.pull_request.pk])
    else:
      trigger = job.event.cause_str()
      trigger_url = reverse('ci:view_event', args=[job.event.pk])

    job_info = {
      'id': job.pk,
      'status': job.status_slug(),
      'runtime': str(job.seconds),
      'recipe_name': job.recipe.name,
      'job_url': reverse('ci:view_job', args=[job.pk,]),
      'config': job.config.name,
      'trigger': trigger,
      'trigger_url': trigger_url,
      'repo': str(job.event.base.repo()),
      'user': str(job.event.head.user()),
      'last_modified': display_time_str(job.last_modified),
      'created': display_time_str(job.created),
      'last_modified_date': sortable_time_str(job.last_modified),
      'client_name': '',
      'client_url': '',
      }
    if job.client:
      job_info['client_name'] = job.client.name
      job_info['client_url'] = reverse('ci:view_client', args=[job.client.pk,])
    ret.append(job_info)
  return ret

def main(request):
  """
  Main view. Just shows the status of repos, with open prs, as
  well as a short list of recent jobs.
  """
  repos = get_repos_status()
  events = models.Event.objects.order_by('-created')[:30]
  return render( request,
      'ci/main.html',
      {'repos': repos,
        'recent_events': events,
        'last_request': int(time.time()),
        'event_limit': 30,
      })

def view_pr(request, pr_id):
  """
  Show the details of a PR
  """
  pr = get_object_or_404(models.PullRequest, pk=pr_id)
  return render(request, 'ci/pr.html', {'pr': pr,})

def is_allowed_to_cancel(session, ev):
  auth = ev.base.server().auth()
  repo = ev.base.branch.repository
  user = auth.signed_in_user(repo.user.server, session)
  if user:
    api = repo.user.server.api()
    auth_session = auth.start_session(session)
    if api.is_collaborator(auth_session, user, repo):
      return True
    logger.info('User {} not a collaborator on {}'.format(user, repo))
  return False

def job_permissions(session, job):
  """
  Logic for a job to see who can see results, activate,
  cancel, invalidate, or owns the job.
  """
  auth = job.event.base.server().auth()
  repo = job.recipe.repository
  user = auth.signed_in_user(repo.user.server, session)
  can_see_results = not job.recipe.private
  can_admin = False
  is_owner = False
  can_activate = False
  if user:
    if job.recipe.automatic == models.Recipe.AUTO_FOR_AUTHORIZED:
      if user in job.recipe.auto_authorized.all():
        can_activate = True

    api = repo.user.server.api()
    auth_session = auth.start_session(session)
    collab = api.is_collaborator(auth_session, user, repo)
    if collab:
      can_admin = True
      can_see_results = True
      is_owner = user == job.recipe.creator
      can_activate = True
  can_see_client = is_allowed_to_see_clients(session)

  return {'is_owner': is_owner,
      'can_see_results': can_see_results,
      'can_admin': can_admin,
      'can_activate': can_activate,
      'can_see_client': can_see_client,
      }

def view_event(request, event_id):
  """
  Show the details of an Event
  """
  ev = get_object_or_404(models.Event, pk=event_id)
  allowed_to_cancel = is_allowed_to_cancel(request.session, ev)
  return render(request, 'ci/event.html', {'event': ev, 'events': [ev], 'allowed_to_cancel': allowed_to_cancel})

def view_job(request, job_id):
  """
  View the details of a job, along
  with any results.
  """
  job = get_object_or_404(models.Job, pk=job_id)
  perms = job_permissions(request.session, job)
  perms['job'] = job
  return render(request, 'ci/job.html', perms)

def get_paginated(request, obj_list, obj_per_page=30):
  paginator = Paginator(obj_list, obj_per_page)

  page = request.GET.get('page')
  try:
    objs = paginator.page(page)
  except PageNotAnInteger:
    # If page is not an integer, deliver first page.
    objs = paginator.page(1)
  except EmptyPage:
    # If page is out of range (e.g. 9999), deliver last page of results.
    objs = paginator.page(paginator.num_pages)
  return objs

def view_repo(request, repo_id):
  """
  View details about a repository, along with
  some recent jobs for each branch.
  """
  repo = get_object_or_404(models.Repository, pk=repo_id)

  branch_info = []
  for branch in repo.branches.exclude(status=models.JobStatus.NOT_STARTED).all():
    events = models.Event.objects.filter(base__branch=branch).order_by('-last_modified')[:30]
    branch_info.append( {'branch': branch, 'events': events} )

  return render(request, 'ci/repo.html', {'repo': repo, 'branch_infos': branch_info})

def view_client(request, client_id):
  """
  View details about a client, along with
  some a list of paginated jobs it has run
  """
  client = get_object_or_404(models.Client, pk=client_id)

  allowed = is_allowed_to_see_clients(request.session)
  if not allowed:
    return render(request, 'ci/client.html', {'client': None, 'allowed': False})

  jobs_list = models.Job.objects.filter(client=client).order_by('-last_modified').all()
  jobs = get_paginated(request, jobs_list)
  return render(request, 'ci/client.html', {'client': client, 'jobs': jobs, 'allowed': True})

def view_branch(request, branch_id):
  branch = get_object_or_404(models.Branch, pk=branch_id)
  event_list = models.Event.objects.filter(base__branch=branch).order_by('-last_modified').all()
  events = get_paginated(request, event_list)
  return render(request, 'ci/branch.html', {'branch': branch, 'events': events})

def job_list(request):
  jobs_list = models.Job.objects.order_by('-last_modified').all()
  jobs = get_paginated(request, jobs_list)
  return render(request, 'ci/jobs.html', {'jobs': jobs})

def pr_list(request):
  pr_list = models.PullRequest.objects.order_by('-last_modified').all()
  prs = get_paginated(request, pr_list)
  return render(request, 'ci/prs.html', {'prs': prs})

def branch_list(request):
  branch_list = models.Branch.objects.exclude(status=models.JobStatus.NOT_STARTED).order_by('repository').all()
  branches = get_paginated(request, branch_list)
  return render(request, 'ci/branches.html', {'branches': branches})

def is_allowed_to_see_clients(session):
  for server in settings.INSTALLED_GITSERVERS:
    gitserver = models.GitServer.objects.get(host_type=server)
    auth = gitserver.auth()
    user = auth.signed_in_user(gitserver, session)
    if not user:
      continue
    api = gitserver.api()
    auth_session = auth.start_session(session)
    for owner in settings.AUTHORIZED_OWNERS:
      owner_obj = models.GitUser.objects.filter(server=gitserver, name=owner).first()
      if not owner_obj:
        continue
      repo_obj = models.Repository.objects.filter(user=owner_obj).first()
      if not repo_obj:
        continue
      if api.is_collaborator(auth_session, user, repo_obj):
        return True
  return False

def client_list(request):
  allowed = is_allowed_to_see_clients(request.session)
  if not allowed:
    return render(request, 'ci/clients.html', {'clients': None, 'allowed': False})

  client_list = models.Client.objects.order_by('-last_seen').all()
  clients = get_paginated(request, client_list)
  return render(request, 'ci/clients.html', {'clients': clients, 'allowed': True})

def event_list(request):
  event_list = models.Event.objects.order_by('-created').all()
  events = get_paginated(request, event_list)
  return render(request, 'ci/events.html', {'events': events})

def recipe_events(request, recipe_id):
  recipe = get_object_or_404(models.Recipe, pk=recipe_id)
  event_list = models.Event.objects.filter(jobs__recipe=recipe).order_by('-last_modified').all()
  total = 0
  count = 0
  qs = models.Job.objects.filter(recipe=recipe)
  for job in qs.all():
    if job.status == models.JobStatus.SUCCESS:
      total += job.seconds.total_seconds()
      count += 1
  if count:
    total /= count
  events = get_paginated(request, event_list)
  avg = timedelta(seconds=total)
  return render(request, 'ci/recipe_events.html', {'recipe': recipe, 'events': events, 'average_time': avg })

def invalidate_job(request, job):
  job.complete = False
  job.event.complete = False
  job.seconds = timedelta(seconds=0)
  job.client = None
  job.active = True
  job.status = models.JobStatus.NOT_STARTED
  job.step_results.all().delete()
  job.save()
  event.make_jobs_ready(job.event)
  messages.info(request, 'Job results invalidated for {}'.format(job))

def invalidate_event(request, event_id):
  """
  Invalidate all the jobs of an event.
  The user must be signed in.
  """
  if request.method != 'POST':
    return HttpResponseNotAllowed(['POST'])

  ev = get_object_or_404(models.Event, pk=event_id)
  allowed = is_allowed_to_cancel(request.session, ev)
  if not allowed:
    raise PermissionDenied('You need to be signed in to invalidate results.')

  for job in ev.jobs.all():
    invalidate_job(request, job)
  ev.complete = False
  ev.status = models.JobStatus.NOT_STARTED
  ev.save()

  return redirect('ci:view_event', event_id=ev.pk)

def invalidate(request, job_id):
  """
  Invalidate the results of a Job.
  The user must be signed in.
  """
  if request.method != 'POST':
    return HttpResponseNotAllowed(['POST'])

  job = get_object_or_404(models.Job, pk=job_id)
  allowed = is_allowed_to_cancel(request.session, job.event)
  if not allowed:
    raise PermissionDenied('You are not allowed to invalidate results.')

  invalidate_job(request, job)
  return redirect('ci:view_job', job_id=job.pk)

def view_profile(request, server_type):
  """
  View the user's profile.
  """
  server = get_object_or_404(models.GitServer, host_type=server_type)
  auth = server.auth()
  user = auth.signed_in_user(server, request.session)
  api = server.api()
  if not user:
    request.session['source_url'] = request.build_absolute_uri()
    return redirect(server.api().sign_in_url())

  auth_session = auth.start_session(request.session)
  repos = api.get_repos(auth_session, request.session)
  org_repos = api.get_org_repos(auth_session, request.session)
  recipes = models.Recipe.objects.filter(creator=user).order_by('repository__name', '-last_modified')
  events = models.Event.objects.filter(build_user=user).order_by('-last_modified')[:30]
  return render(request, 'ci/profile.html', {
    'user': user,
    'repos': repos,
    'org_repos': org_repos,
    'recipes': recipes,
    'events': events,
    })


@csrf_exempt
def manual_branch(request, build_key, branch_id):
  """
  Endpoint for creating a manual event.
  """
  if request.method != 'POST':
    return HttpResponseNotAllowed(['POST'])

  branch = get_object_or_404(models.Branch, pk=branch_id)
  user = get_object_or_404(models.GitUser, build_key=build_key)
  reply = 'OK'
  try:
    logger.info('Running manual with user %s on branch %s' % (user, branch))
    oauth_session = user.start_session()
    latest = user.api().last_sha(oauth_session, branch.repository.user.name, branch.repository.name, branch.name)
    if latest:
      mev = event.ManualEvent(user, branch, latest)
      mev.save(request)
      reply = 'Success. Scheduled recipes on branch %s for user %s' % (branch, user)
      messages.info(request, reply)
  except Exception as e:
    reply = 'Error running manual for build_key %s on branch %s\nError: %s'\
        % (build_key, branch, traceback.format_exc(e))
    messages.error(request, reply)

  logger.info(reply)
  next_url = request.POST.get('next', None)
  if next_url:
    return redirect(next_url)
  return HttpResponse(reply)

def activate_job(request, job_id):
  """
  Endpoint for creating a manual event.
  """
  if request.method != 'POST':
    return HttpResponseNotAllowed(['POST'])

  job = get_object_or_404(models.Job, pk=job_id)
  owner = job.recipe.repository.user
  auth = owner.server.auth()
  user = auth.signed_in_user(owner.server, request.session)
  if not user:
    raise PermissionDenied('You need to be signed in to activate a job')

  auth_session = auth.start_session(request.session)
  if owner.server.api().is_collaborator(auth_session, user, job.recipe.repository):
    job.active = True
    job.status = models.JobStatus.NOT_STARTED
    job.event.status = models.JobStatus.NOT_STARTED
    job.event.complete = False
    job.event.save()
    job.save()
    event.make_jobs_ready(job.event)
    messages.info(request, 'Job activated')
  else:
    raise PermissionDenied('%s is not a collaborator on %s' % (user, job.recipe.repository))

  return redirect('ci:view_job', job_id=job.pk)


def cancel_event(request, event_id):
  if request.method != 'POST':
    return HttpResponseNotAllowed(['POST'])

  ev = get_object_or_404(models.Event, pk=event_id)
  if is_allowed_to_cancel(request.session, ev):
    event.cancel_event(ev)
    messages.info(request, 'Event {} canceled'.format(ev))
  else:
    return HttpResponseForbidden('Not allowed to cancel this event')

  return redirect('ci:view_event', event_id=ev.pk)

def cancel_job(request, job_id):
  if request.method != 'POST':
    return HttpResponseNotAllowed(['POST'])

  job = get_object_or_404(models.Job, pk=job_id)
  if is_allowed_to_cancel(request.session, job.event):
    job.status = models.JobStatus.CANCELED
    job.complete = True
    job.save()
    job.event.status = models.JobStatus.CANCELED
    job.event.save()
    messages.info(request, 'Job {} canceled'.format(str(job)))
  else:
    return HttpResponseForbidden('Not allowed to cancel this job')
  return redirect('ci:view_job', job_id=job.pk)


def start_session_by_name(request, name):
  if not settings.DEBUG:
    raise Http404()

  user = get_object_or_404(models.GitUser, name=name)
  if not user.token:
    raise Http404('User %s does not have a token.' % user.name )
  user.server.auth().set_browser_session_from_user(request.session, user)
  messages.info(request, "Started session")
  return redirect('ci:main')

def start_session(request, user_id):
  if not settings.DEBUG:
    raise Http404()

  user = get_object_or_404(models.GitUser, pk=user_id)
  if not user.token:
    raise Http404('User %s does not have a token.' % user.name )
  user.server.auth().set_browser_session_from_user(request.session, user)
  messages.info(request, "Started session")
  return redirect('ci:main')


def read_recipe_file(filename):
  fname = '{}/{}'.format(settings.RECIPE_BASE_DIR, filename)
  if not os.path.exists(fname):
    return None
  with open(fname, 'r') as f:
    return f.read()

def job_script(request, job_id):
  job = get_object_or_404(models.Job, pk=job_id)
  perms = job_permissions(request.session, job)
  if not perms['is_owner']:
    raise Http404('Not the owner')
  script = '<pre>#!/bin/bash'
  script += '\n# Script for job {}'.format(job)
  script += '\n# Note that BUILD_ROOT and other environment variables set by the client are not set'
  script += '\n# It is a good idea to redirect stdin, id "./script.sh  < /dev/null"'
  script += '\n\n'
  script += '\nexport BUILD_ROOT=""'
  script += '\nexport MOOSE_JOBS="1"'
  script += '\n\n'
  recipe = job.recipe
  for prestep in recipe.prestepsources.all():
    script += '\n{}\n'.format(read_recipe_file(prestep.filename))

  for env in recipe.environment_vars.all():
    script += '\nexport {}={}'.format(env.name, env.value)

  script += '\nexport recipe_name="{}"'.format(job.recipe.name)
  script += '\nexport job_id="{}"'.format(job.pk)
  script += '\nexport abort_on_failure="{}"'.format(job.recipe.abort_on_failure)
  script += '\nexport recipe_id="{}"'.format(job.recipe.pk)
  script += '\nexport comments_url="{}"'.format(job.event.comments_url)
  script += '\nexport base_repo="{}"'.format(job.event.base.repo())
  script += '\nexport base_ref="{}"'.format(job.event.base.branch.name)
  script += '\nexport base_sha="{}"'.format(job.event.base.sha)
  script += '\nexport base_ssh_url="{}"'.format(job.event.base.ssh_url)
  script += '\nexport head_repo="{}"'.format(job.event.head.repo())
  script += '\nexport head_ref="{}"'.format(job.event.head.branch.name)
  script += '\nexport head_sha="{}"'.format(job.event.head.sha)
  script += '\nexport head_ssh_url="{}"'.format(job.event.head.ssh_url)
  script += '\nexport cause="{}"'.format(job.recipe.cause_str())
  script += '\nexport config="{}"'.format(job.config.name)
  script += '\n\n'

  count = 0
  step_cmds = ''
  for step in recipe.steps.all():
    script += '\nfunction step_{}\n{{'.format(count)
    script += '\n\tstep_num="{}"'.format(step.position)
    script += '\n\tstep_name="{}"'.format(step.name)
    script += '\n\tstep_id="{}"'.format(step.pk)
    script += '\n\tstep_abort_on_failure="{}"'.format(step.abort_on_failure)

    for env in step.step_environment.all():
      script += '\n\t{}="{}"'.format(env.name, env.value)

    for l in read_recipe_file(step.filename).split('\n'):
      script += '\n\t{}'.format(l.replace('exit 0', 'return 0'))
    script += '\n}\n'
    step_cmds += '\nstep_{}'.format(count)
    count += 1

  script += step_cmds
  script += '</pre>'
  return HttpResponse(script)
