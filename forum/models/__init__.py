from question import Question ,QuestionRevision, QuestionView, AnonymousQuestion, FavoriteQuestion
from answer import Answer, AnonymousAnswer, AnswerRevision
from tag import Tag, MarkedTag
from meta import Vote, Comment, FlaggedItem
from user import Activity, ValidationHash, EmailFeedSetting
from user import AuthKeyUserAssociation
from repute import Badge, Award, Repute
import signals
from django.core.urlresolvers import reverse
from django.core.mail import EmailMessage
from forum.search.indexer import create_fulltext_indexes
from django.db.models.signals import post_syncdb
from django.template import loader, Context
from django.utils.translation import ugettext as _
from forum import const
import logging
import re

from base import *
import datetime
from django.contrib.contenttypes.models import ContentType

#todo: must go after signals
from forum import auth

# User extend properties
QUESTIONS_PER_PAGE_CHOICES = (
   (10, u'10'),
   (30, u'30'),
   (50, u'50'),
)

def user_is_username_taken(cls,username):
    try:
        cls.objects.get(username=username)
        return True
    except cls.MultipleObjectsReturned:
        return True
    except cls.DoesNotExist:
        return False

def user_get_q_sel_email_feed_frequency(self):
    #print 'looking for frequency for user %s' % self
    try:
        feed_setting = EmailFeedSetting.objects.get(subscriber=self,feed_type='q_sel')
    except Exception, e:
        #print 'have error %s' % e.message
        raise e
    #print 'have freq=%s' % feed_setting.frequency
    return feed_setting.frequency

def user_get_absolute_url(self):
    return "/users/%d/%s/" % (self.id, (self.username))

User.add_to_class('is_approved', models.BooleanField(default=False))
User.add_to_class('email_isvalid', models.BooleanField(default=False))
User.add_to_class('email_key', models.CharField(max_length=32, null=True))

#hardcoded initial reputaion of 1, no setting for this one
User.add_to_class('reputation', models.PositiveIntegerField(default=1))
User.add_to_class('gravatar', models.CharField(max_length=32))

#User.add_to_class('favorite_questions',
#                  models.ManyToManyField(Question, through=FavoriteQuestion,
#                                         related_name='favorited_by'))

#User.add_to_class('badges', models.ManyToManyField(Badge, through=Award,
#                                                   related_name='awarded_to'))
User.add_to_class('gold', models.SmallIntegerField(default=0))
User.add_to_class('silver', models.SmallIntegerField(default=0))
User.add_to_class('bronze', models.SmallIntegerField(default=0))
User.add_to_class('questions_per_page',
                  models.SmallIntegerField(choices=QUESTIONS_PER_PAGE_CHOICES, default=10))
User.add_to_class('last_seen',
                  models.DateTimeField(default=datetime.datetime.now))
User.add_to_class('real_name', models.CharField(max_length=100, blank=True))
User.add_to_class('website', models.URLField(max_length=200, blank=True))
User.add_to_class('location', models.CharField(max_length=100, blank=True))
User.add_to_class('date_of_birth', models.DateField(null=True, blank=True))
User.add_to_class('about', models.TextField(blank=True))
User.add_to_class('is_username_taken',classmethod(user_is_username_taken))
User.add_to_class('get_q_sel_email_feed_frequency',user_get_q_sel_email_feed_frequency)
User.add_to_class('hide_ignored_questions', models.BooleanField(default=False))
User.add_to_class('tag_filter_setting',
                    models.CharField(
                                        max_length=16,
                                        choices=TAG_EMAIL_FILTER_CHOICES,
                                        default='ignored'
                                     )
                 )
User.add_to_class('get_absolute_url', user_get_absolute_url)

def get_messages(self):
    messages = []
    for m in self.message_set.all():
        messages.append(m.message)
    return messages

def delete_messages(self):
    self.message_set.all().delete()

def get_profile_url(self):
    """Returns the URL for this User's profile."""
    return reverse('user_profile', kwargs={'id':self.id, 'slug':slugify(self.username)})

def get_profile_link(self):
    profile_link = u'<a href="%s">%s</a>' % (self.get_profile_url(),self.username)
    return mark_safe(profile_link)

#series of methods for user vote-type commands
#same call signature func(self, post, timestamp=None, cancel=None)
#note that none of these have business logic checks internally
#these functions are used by the forum app and
#by the data importer jobs from say stackexchange, where internal rules
#may be different
#maybe if we do use business rule checks here - we should add
#some flag allowing to bypass them for things like the data importers
def toggle_favorite_question(self, question, timestamp=None, cancel=False):
    """cancel has no effect here, but is important for the SE loader
    it is hoped that toggle will work and data will be consistent
    but there is no guarantee, maybe it's better to be more strict 
    about processing the "cancel" option
    another strange thing is that this function unlike others below
    returns a value
    """
    try:
        fave = FavoriteQuestion.objects.get(question=question, user=self)
        fave.delete()
        result = False
    except FavoriteQuestion.DoesNotExist:
        if timestamp is None:
            timestamp = datetime.datetime.now()
        fave = FavoriteQuestion(
            question = question,
            user = self,
            added_at = timestamp,
        )
        fave.save()
        result = True
    Question.objects.update_favorite_count(question)
    return result

#"private" wrapper function that applies post upvotes/downvotes and cancelations
def _process_vote(user, post, timestamp=None, cancel=False, vote_type=None):
    post_type = ContentType.objects.get_for_model(post)
    #get or create the vote object
    #return with noop in some situations
    try:
        vote = Vote.objects.get(
                    user = user,
                    content_type = post_type,
                    object_id = post.id,
                )
    except Vote.DoesNotExist:
        vote = None
    if cancel:
        if vote == None:
            return
        elif vote.is_opposite(vote_type):
            return
        else:
            #we would call vote.delete() here
            #but for now all that is handled by the
            #legacy forum.auth functions
            #vote.delete()
            pass
    else:
        if vote == None:
            vote = Vote(
                    user = user,
                    content_object = post,
                    vote = vote_type,
                    voted_at = timestamp,
                    )
        elif vote.is_opposite(vote_type):
            vote.vote = vote_type
        else:
            return

    #do the actual work
    if vote_type == Vote.VOTE_UP:
        if cancel:
            auth.onUpVotedCanceled(vote, post, user, timestamp)
        else:
            auth.onUpVoted(vote, post, user, timestamp)
    elif vote_type == Vote.VOTE_DOWN:
        if cancel:
            auth.onDownVotedCanceled(vote, post, user, timestamp)
        else:
            auth.onDonwVoted(vote, post, user, timestamp)

def upvote(self, post, timestamp=None, cancel=False):
    _process_vote(
        self,post,
        timestamp=timestamp,
        cancel=cancel,
        vote_type=Vote.VOTE_UP
    )

def downvote(self, post, timestamp=None, cancel=False):
    _process_vote(
        self,post,
        timestamp=timestamp,
        cancel=cancel,
        vote_type=Vote.VOTE_DOWN
    )

def accept_answer(self, answer, timestamp=None, cancel=False):
    if cancel:
        auth.onAnswerAcceptCanceled(answer, self, timestamp=timestamp)
    else:
        auth.onAnswerAccept(answer, self, timestamp=timestamp)

def flag_post(self, post, timestamp=None, cancel=False):
    if cancel:#todo: can't unflag?
        return
    if post.flagged_items.filter(user=user).count() > 0:
        return
    else:
        flag = FlaggedItem(
                user = self,
                content_object = post,
                flagged_at = timestamp,
            )
        auth.onFlaggedItem(flag, post, user, timestamp=timestamp)

def user_should_receive_instant_notification_about_post(user, post):
    return EmailFeedSetting.objects.exists_match_to_post_and_subscriber(
                                            subscriber = user,
                                            post = post,
                                            frequency = 'i',
                                       )


User.add_to_class('upvote', upvote)
User.add_to_class('downvote', downvote)
User.add_to_class('accept_answer', accept_answer)
User.add_to_class('flag_post', flag_post)
User.add_to_class('get_profile_url', get_profile_url)
User.add_to_class('get_profile_link', get_profile_link)
User.add_to_class('get_messages', get_messages)
User.add_to_class('delete_messages', delete_messages)
User.add_to_class('toggle_favorite_question', toggle_favorite_question)
User.add_to_class(
        'should_receive_instant_notification_about_post', 
        user_should_receive_instant_notification_about_post
    )

def calculate_gravatar_hash(instance, **kwargs):
    """Calculates a User's gravatar hash from their email address."""
    if kwargs.get('raw', False):
        return
    instance.gravatar = hashlib.md5(instance.email).hexdigest()

def record_ask_event(instance, created, **kwargs):
    if created:
        activity = Activity(
                        user=instance.author, 
                        active_at=instance.added_at, 
                        content_object=instance, 
                        activity_type=const.TYPE_ACTIVITY_ASK_QUESTION
                    )
        activity.save()

#todo: translate this
record_answer_event_re = re.compile("You have received (a|\d+) .*new response.*")
def record_answer_event(instance, created, **kwargs):
    if created:
        q_author = instance.question.author
        found_match = False
        for m in q_author.message_set.all():
            match = record_answer_event_re.search(m.message)
            if match:
                found_match = True
                try:
                    cnt = int(match.group(1))
                except:
                    cnt = 1
                m.message = u"You have received %d <a href=\"%s?sort=responses\">new responses</a>."\
                            % (cnt+1, q_author.get_profile_url())
                m.save()
                break
        if not found_match:
            msg = u"You have received a <a href=\"%s?sort=responses\">new response</a>."\
                    % q_author.get_profile_url()
            q_author.message_set.create(message=msg)

        activity = Activity(
                        user = instance.author,
                        active_at = instance.added_at,
                        content_object = instance,
                        activity_type = const.TYPE_ACTIVITY_ANSWER
                    )
        activity.save()
        receiving_users = instance.question.get_author_list(
                                                    include_comments = True,
                                                    exclude_list = [instance.author],
                                                )

        activity.receiving_users.add(*receiving_users)


def record_comment_event(instance, created, **kwargs):
    if created:
        if isinstance(instance.content_object, Question):
            activity_type = const.TYPE_ACTIVITY_COMMENT_QUESTION
        elif isinstance(instance.content_object, Answer):
            activity_type = const.TYPE_ACTIVITY_COMMENT_ANSWER
        else:
            logging.critical(
                        'recording comment for %s is not implemented'\
                        % type(instance.content_object)
                    )

        activity = Activity(
                        user = instance.user, 
                        active_at = instance.added_at, 
                        content_object = instance, 
                        activity_type = activity_type
                    )
        activity.save()

        receiving_users = instance.content_object.get_author_list(
                                        include_comments = True,
                                        exclude_list = [instance.user],
                                    )
        activity.receiving_users.add(*receiving_users)
        #todo: remove this upon migration to 1.2
        signals.fake_m2m_changed.send(sender = Activity, instance = activity, created = True)


def record_revision_question_event(instance, created, **kwargs):
    if created and instance.revision <> 1:
        activity = Activity(
                        user=instance.author,
                        active_at=instance.revised_at,
                        content_object=instance,
                        activity_type=const.TYPE_ACTIVITY_UPDATE_QUESTION
                    )
        activity.save()
        receiving_users = set()
        receiving_users.update(
                            instance.question.get_author_list(include_comments = True)
                        )
        for a in instance.question.answers:
            receiving_users.update(a.get_author_list())
        receiving_users -= set([instance.author])

        receiving_users = list(receiving_users)
        activity.receiving_users.add(*receiving_users)


def record_revision_answer_event(instance, created, **kwargs):
    if created and instance.revision <> 1:
        activity = Activity(
                        user=instance.author, 
                        active_at=instance.revised_at, 
                        content_object=instance, 
                        activity_type=const.TYPE_ACTIVITY_UPDATE_ANSWER
                    )
        activity.save()
        receiving_users = set()
        receiving_users.update(
                            instance.answer.get_author_list(
                                            including_comments = True 
                                        )
                        )
        receiving_users.update(instance.answer.question.get_author_list())
        receiving_users -= set([instance.author])
        receiving_users = list(receiving_users)

        activity.receiving_users.add(*receiving_users)


def maybe_send_instant_notifications(instance, created, **kwargs):
    """todo: this handler must change when we switch to django 1.2
    """
    activity_instance = instance
    if not created:
        return
    activity_type = activity_instance.activity_type
    if activity_type not in const.RESPONSE_ACTIVITY_TYPES_FOR_EMAIL:
        return

    #todo: remove this after migrating to string type for const.TYPE_ACTIVITY...
    update_type_map = {
                    const.TYPE_ACTIVITY_COMMENT_QUESTION: 'question_comment',
                    const.TYPE_ACTIVITY_COMMENT_ANSWER: 'answer_comment',
                    const.TYPE_ACTIVITY_UPDATE_ANSWER: 'answer_update',
                    const.TYPE_ACTIVITY_UPDATE_QUESTION: 'question_update',
                    const.TYPE_ACTIVITY_MENTION: 'mention',
                }

    post = activity_instance.get_response_type_content_object()
    template = loader.get_template('instant_notification.html')
    for u in activity_instance.receiving_users.all():
        if u.should_receive_instant_notification_about_post(post):
            
            mentions = Activity.objects.get_mentions(
                                        mentioned_whom = u,
                                        mentioned_in = post,
                                        reported = False
                                    )
            if mentions:
                #todo: find a more semantic way to do this
                mentions.update(is_auditted = True)
                has_mention = True
            else:
                has_mention = False

            #get details about update
            #todo: is there a way to solve this import issue?
            from forum.conf import settings as forum_settings
            data = {
                'receiving_user': u,
                'update_author': activity_instance.user,
                'updated_post': post,
                'update_url': forum_settings.APP_URL + post.get_absolute_url(),
                'update_type': update_type_map[activity_type],
                'revision_number': post.get_latest_revision_number(),
                'related_origin_post': post.get_origin_post(),
                'has_mention': has_mention,
            }
            #send update
            subject = _('email update message subject')
            text = template.render(Context(data)) 
            msg = EmailMessage(subject, text, settings.DEFAULT_FROM_EMAIL, [u.email])
            #print 'sending email to %s' % u.email
            #print 'subject: %s' % subject
            #print 'body: %s' % text
            #msg.send()

def record_award_event(instance, created, **kwargs):
    """
    After we awarded a badge to user, we need to record this activity and notify user.
    We also recaculate awarded_count of this badge and user information.
    """
    if created:
        activity = Activity(
                        user=instance.user,#todo: change this to community user who gives the award
                        active_at=instance.awarded_at,
                        content_object=instance,
                        activity_type=const.TYPE_ACTIVITY_PRIZE
                    )
        activity.save()
        activity.receiving_users.add(instance.user)

        instance.badge.awarded_count += 1
        instance.badge.save()

        if instance.badge.type == Badge.GOLD:
            instance.user.gold += 1
        if instance.badge.type == Badge.SILVER:
            instance.user.silver += 1
        if instance.badge.type == Badge.BRONZE:
            instance.user.bronze += 1
        instance.user.save()

def notify_award_message(instance, created, **kwargs):
    """
    Notify users when they have been awarded badges by using Django message.
    """
    if created:
        user = instance.user

        msg = (u"Congratulations, you have received a badge '%s'. " \
                + u"Check out <a href=\"%s\">your profile</a>.") \
                % (instance.badge.name, user.get_profile_url())

        user.message_set.create(message=msg)

def record_answer_accepted(instance, created, **kwargs):
    """
    when answer is accepted, we record this for question author - who accepted it.
    """
    if not created and instance.accepted:
        activity = Activity(
                        user=instance.question.author,
                        active_at=datetime.datetime.now(),
                        content_object=instance,
                        activity_type=const.TYPE_ACTIVITY_MARK_ANSWER
                    )
        receiving_users = instance.get_author_list(
                                            exclude_list = [instance.question.author]
                                        )
        activity.receiving_users.add(*receiving_users)
        activity.save()

def update_last_seen(instance, created, **kwargs):
    """
    when user has activities, we update 'last_seen' time stamp for him
    """
    #todo: improve this
    user = instance.user
    user.last_seen = datetime.datetime.now()
    user.save()

def record_vote(instance, created, **kwargs):
    """
    when user have voted
    """
    if created:
        if instance.vote == 1:
            vote_type = const.TYPE_ACTIVITY_VOTE_UP
        else:
            vote_type = const.TYPE_ACTIVITY_VOTE_DOWN

        activity = Activity(
                        user=instance.user,
                        active_at=instance.voted_at,
                        content_object=instance,
                        activity_type=vote_type
                    )
        #todo: problem cannot access receiving user here
        activity.save()

def record_cancel_vote(instance, **kwargs):
    """
    when user canceled vote, the vote will be deleted.
    """
    activity = Activity(
                    user=instance.user, 
                    active_at=datetime.datetime.now(), 
                    content_object=instance, 
                    activity_type=const.TYPE_ACTIVITY_CANCEL_VOTE
                )
    #todo: same problem - cannot access receiving user here
    activity.save()

def record_delete_question(instance, delete_by, **kwargs):
    """
    when user deleted the question
    """
    if instance.__class__ == "Question":
        activity_type = const.TYPE_ACTIVITY_DELETE_QUESTION
    else:
        activity_type = const.TYPE_ACTIVITY_DELETE_ANSWER

    activity = Activity(
                    user=delete_by, 
                    active_at=datetime.datetime.now(), 
                    content_object=instance, 
                    activity_type=activity_type
                )
    #no need to set receiving user here
    activity.save()

def record_mark_offensive(instance, mark_by, **kwargs):
    activity = Activity(
                    user=mark_by, 
                    active_at=datetime.datetime.now(), 
                    content_object=instance, 
                    activity_type=const.TYPE_ACTIVITY_MARK_OFFENSIVE
                )
    activity.save()
    receiving_users = instance.get_author_list(
                                        exclude_list = [mark_by]
                                    )
    activity.receiving_users.add(*receiving_users)

def record_update_tags(question, **kwargs):
    """
    when user updated tags of the question
    """
    activity = Activity(
                    user=question.author,
                    active_at=datetime.datetime.now(),
                    content_object=question,
                    activity_type=const.TYPE_ACTIVITY_UPDATE_TAGS
                )
    activity.save()

def record_favorite_question(instance, created, **kwargs):
    """
    when user add the question in him favorite questions list.
    """
    if created:
        activity = Activity(
                        user=instance.user, 
                        active_at=datetime.datetime.now(), 
                        content_object=instance, 
                        activity_type=const.TYPE_ACTIVITY_FAVORITE
                    )
        activity.save()
        receiving_users = instance.question.get_author_list(
                                                    exclude_list = [instance.user]
                                                )
        activity.receiving_users.add(*receiving_users)

def record_user_full_updated(instance, **kwargs):
    activity = Activity(
                    user=instance, 
                    active_at=datetime.datetime.now(), 
                    content_object=instance, 
                    activity_type=const.TYPE_ACTIVITY_USER_FULL_UPDATED
                )
    activity.save()

def post_stored_anonymous_content(sender,user,session_key,signal,*args,**kwargs):
    aq_list = AnonymousQuestion.objects.filter(session_key = session_key)
    aa_list = AnonymousAnswer.objects.filter(session_key = session_key)
    from forum.conf import settings as forum_settings
    if forum_settings.EMAIL_VALIDATION == True:#add user to the record
        for aq in aq_list:
            aq.author = user
            aq.save()
        for aa in aa_list:
            aa.author = user
            aa.save()
        #maybe add pending posts message?
    else: #just publish the questions
        for aq in aq_list:
            aq.publish(user)
        for aa in aa_list:
            aa.publish(user)

#signal for User model save changes
pre_save.connect(calculate_gravatar_hash, sender=User)
post_save.connect(record_ask_event, sender=Question)
post_save.connect(record_answer_event, sender=Answer)
post_save.connect(record_comment_event, sender=Comment)
post_save.connect(record_revision_question_event, sender=QuestionRevision)
post_save.connect(record_revision_answer_event, sender=AnswerRevision)
post_save.connect(record_award_event, sender=Award)
post_save.connect(notify_award_message, sender=Award)
post_save.connect(record_answer_accepted, sender=Answer)
post_save.connect(update_last_seen, sender=Activity)
post_save.connect(record_vote, sender=Vote)
post_save.connect(record_favorite_question, sender=FavoriteQuestion)
post_delete.connect(record_cancel_vote, sender=Vote)

#change this to real m2m_changed with Django1.2
signals.fake_m2m_changed.connect(maybe_send_instant_notifications, sender=Activity)
signals.delete_post_or_answer.connect(record_delete_question, sender=Question)
signals.delete_post_or_answer.connect(record_delete_question, sender=Answer)
signals.mark_offensive.connect(record_mark_offensive, sender=Question)
signals.mark_offensive.connect(record_mark_offensive, sender=Answer)
signals.tags_updated.connect(record_update_tags, sender=Question)
signals.user_updated.connect(record_user_full_updated, sender=User)
signals.user_logged_in.connect(post_stored_anonymous_content)
#post_syncdb.connect(create_fulltext_indexes)

#todo: wtf??? what is x=x about?
signals = signals

Question = Question
QuestionRevision = QuestionRevision
QuestionView = QuestionView
FavoriteQuestion = FavoriteQuestion
AnonymousQuestion = AnonymousQuestion

Answer = Answer
AnswerRevision = AnswerRevision
AnonymousAnswer = AnonymousAnswer

Tag = Tag
Comment = Comment
Vote = Vote
FlaggedItem = FlaggedItem
MarkedTag = MarkedTag

Badge = Badge
Award = Award
Repute = Repute

Activity = Activity
EmailFeedSetting = EmailFeedSetting
ValidationHash = ValidationHash
AuthKeyUserAssociation = AuthKeyUserAssociation

__all__ = [
        'signals',

        'Question',
        'QuestionRevision',
        'QuestionView',
        'FavoriteQuestion',
        'AnonymousQuestion',

        'Answer',
        'AnswerRevision',
        'AnonymousAnswer',

        'Tag',
        'Comment',
        'Vote',
        'FlaggedItem',
        'MarkedTag',

        'Badge',
        'Award',
        'Repute',

        'Activity',
        'EmailFeedSetting',
        'ValidationHash',
        'AuthKeyUserAssociation',

        'User',
        ]


from forum.modules import get_modules_script_classes

for k, v in get_modules_script_classes('models', models.Model).items():
    if not k in __all__:
        __all__.append(k)
        exec "%s = v" % k
