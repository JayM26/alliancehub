from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.models import Group, Permission
from django import forms

# Only show permissions from these apps
VISIBLE_APPS = {
    "srp",
    # add more later: "sso", "fleet", etc.
}

# Hide Django's default add/change/delete/view perms
HIDE_DEFAULT_MODEL_PERMS = True


class FilteredGroupAdminForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        qs = Permission.objects.select_related("content_type").all()

        qs = qs.filter(content_type__app_label__in=VISIBLE_APPS)

        if HIDE_DEFAULT_MODEL_PERMS:
            qs = qs.exclude(codename__startswith="add_")
            qs = qs.exclude(codename__startswith="change_")
            qs = qs.exclude(codename__startswith="delete_")
            qs = qs.exclude(codename__startswith="view_")

        self.fields["permissions"].queryset = qs.order_by(
            "content_type__app_label",
            "content_type__model",
            "codename",
        )


# Replace default Group admin with filtered version
admin.site.unregister(Group)


@admin.register(Group)
class GroupAdmin(DjangoGroupAdmin):
    form = FilteredGroupAdminForm
