FROM node:11
RUN apt-get update \
    && apt-get install -y lsof netcat dos2unix \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN npm install \
    && npm install -g typescript \
    && npm install -g ts-api
RUN mkdir -p dist \
    && mkdir -p docs
RUN chmod +x entrypoint.sh
RUN tsc
RUN cg
RUN mkdir bin
RUN dos2unix -n src/bin/cb_proxy bin/cb_proxy
EXPOSE 63100
CMD bash entrypoint.sh
