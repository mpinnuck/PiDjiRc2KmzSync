.PHONY: deploy zip help

help:
	@printf '%s\n' \
		'make deploy             Deploy the code to the Raspberry Pi' \
		'make deploy RESTART=1  Deploy and restart the Gunicorn service' \
		'make deploy-restart    Deploy and restart the Gunicorn service' \
		'make zip               Create the source zip archive'

deploy:
	./deploy.sh $(if $(RESTART),--restart,)

deploy-restart:
	./deploy.sh --restart

zip:
	../zipsource.sh