"""Defines serializers used by the Team API."""
from copy import deepcopy

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Count
from django_countries import countries
from rest_framework import serializers

from lms.djangoapps.teams.models import CourseTeam, CourseTeamMembership
from openedx.core.djangoapps.user_api.accounts.serializers import UserReadOnlySerializer
from openedx.core.lib.api.fields import ExpandableField
from openedx.core.lib.api.serializers import CollapsedReferenceSerializer


class CountryField(serializers.Field):
    """
    Field to serialize a country code.
    """

    COUNTRY_CODES = dict(countries).keys()

    def to_representation(self, obj):
        """
        Represent the country as a 2-character unicode identifier.
        """
        return unicode(obj)

    def to_internal_value(self, data):
        """
        Check that the code is a valid country code.

        We leave the data in its original format so that the Django model's
        CountryField can convert it to the internal representation used
        by the django-countries library.
        """
        if data and data not in self.COUNTRY_CODES:
            raise serializers.ValidationError(
                u"{code} is not a valid country code".format(code=data)
            )
        return data


class UserMembershipSerializer(serializers.ModelSerializer):
    """Serializes CourseTeamMemberships with only user and date_joined

    Used for listing team members.
    """
    profile_configuration = deepcopy(settings.ACCOUNT_VISIBILITY_CONFIGURATION)
    profile_configuration['bulk_shareable_fields'].append('url')
    profile_configuration['public_fields'].append('url')

    user = ExpandableField(
        collapsed_serializer=CollapsedReferenceSerializer(
            model_class=User,
            id_source='username',
            view_name='accounts_api',
            read_only=True,
        ),
        expanded_serializer=UserReadOnlySerializer(configuration=profile_configuration),
    )

    class Meta(object):
        model = CourseTeamMembership
        fields = ("user", "date_joined", "last_activity_at")
        read_only_fields = ("date_joined", "last_activity_at")


class CourseTeamSerializer(serializers.ModelSerializer):
    """Serializes a CourseTeam with membership information."""
    id = serializers.CharField(source='team_id', read_only=True)  # pylint: disable=invalid-name
    membership = UserMembershipSerializer(many=True, read_only=True)
    country = CountryField()

    class Meta(object):
        model = CourseTeam
        fields = (
            "id",
            "discussion_topic_id",
            "name",
            "course_id",
            "topic_id",
            "date_created",
            "description",
            "country",
            "language",
            "last_activity_at",
            "membership",
        )
        read_only_fields = ("course_id", "date_created", "discussion_topic_id", "last_activity_at")


class CourseTeamCreationSerializer(serializers.ModelSerializer):
    """Deserializes a CourseTeam for creation."""

    country = CountryField(required=False)

    class Meta(object):
        model = CourseTeam
        fields = (
            "name",
            "course_id",
            "description",
            "topic_id",
            "country",
            "language",
        )

    def create(self, validated_data):
        team = CourseTeam.create(
            name=validated_data.get("name", ''),
            course_id=validated_data.get("course_id"),
            description=validated_data.get("description", ''),
            topic_id=validated_data.get("topic_id", ''),
            country=validated_data.get("country", ''),
            language=validated_data.get("language", ''),
        )
        team.save()
        return team


class CourseTeamSerializerWithoutMembership(CourseTeamSerializer):
    """The same as the `CourseTeamSerializer`, but elides the membership field.

    Intended to be used as a sub-serializer for serializing team
    memberships, since the membership field is redundant in that case.
    """

    def __init__(self, *args, **kwargs):
        super(CourseTeamSerializerWithoutMembership, self).__init__(*args, **kwargs)
        del self.fields['membership']


class MembershipSerializer(serializers.ModelSerializer):
    """Serializes CourseTeamMemberships with information about both teams and users."""
    profile_configuration = deepcopy(settings.ACCOUNT_VISIBILITY_CONFIGURATION)
    profile_configuration['bulk_shareable_fields'].append('url')
    profile_configuration['public_fields'].append('url')

    user = ExpandableField(
        collapsed_serializer=CollapsedReferenceSerializer(
            model_class=User,
            id_source='username',
            view_name='accounts_api',
            read_only=True,
        ),
        expanded_serializer=UserReadOnlySerializer(configuration=profile_configuration)
    )
    team = ExpandableField(
        collapsed_serializer=CollapsedReferenceSerializer(
            model_class=CourseTeam,
            id_source='team_id',
            view_name='teams_detail',
            read_only=True,
        ),
        expanded_serializer=CourseTeamSerializerWithoutMembership(read_only=True),
    )

    class Meta(object):
        model = CourseTeamMembership
        fields = ("user", "team", "date_joined", "last_activity_at")
        read_only_fields = ("date_joined", "last_activity_at")


class BaseTopicSerializer(serializers.Serializer):
    """Serializes a topic without team_count."""
    description = serializers.CharField()
    name = serializers.CharField()
    id = serializers.CharField()  # pylint: disable=invalid-name


class TopicSerializer(BaseTopicSerializer):
    """
    Adds team_count to the basic topic serializer, checking if team_count
    is already present in the topic data, and if not, querying the CourseTeam
    model to get the count. Requires that `context` is provided with a valid course_id
    in order to filter teams within the course.
    """
    team_count = serializers.SerializerMethodField()

    def get_team_count(self, topic):
        """Get the number of teams associated with this topic"""
        # If team_count is already present (possible if topic data was pre-processed for sorting), return it.
        if 'team_count' in topic:
            return topic['team_count']
        else:
            return CourseTeam.objects.filter(course_id=self.context['course_id'], topic_id=topic['id']).count()


class BulkTeamCountTopicListSerializer(serializers.ListSerializer):  # pylint: disable=abstract-method
    """
    List serializer for efficiently serializing a set of topics.
    """

    def to_representation(self, obj):
        """Adds team_count to each topic. """
        data = super(BulkTeamCountTopicListSerializer, self).to_representation(obj)
        add_team_count(data, self.context["course_id"])
        return data


class BulkTeamCountTopicSerializer(BaseTopicSerializer):  # pylint: disable=abstract-method
    """
    Serializes a set of topics, adding the team_count field to each topic as a bulk operation.
    Requires that `context` is provided with a valid course_id in order to filter teams within the course.
    """
    class Meta(object):
        list_serializer_class = BulkTeamCountTopicListSerializer


def add_team_count(topics, course_id):
    """
    Helper method to add team_count for a list of topics.
    This allows for a more efficient single query.
    """
    topic_ids = [topic['id'] for topic in topics]
    teams_per_topic = CourseTeam.objects.filter(
        course_id=course_id,
        topic_id__in=topic_ids
    ).values('topic_id').annotate(team_count=Count('topic_id'))

    topics_to_team_count = {d['topic_id']: d['team_count'] for d in teams_per_topic}
    for topic in topics:
        topic['team_count'] = topics_to_team_count.get(topic['id'], 0)
