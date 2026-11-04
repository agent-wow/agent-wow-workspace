FROM golang:1.27.1 AS build
ENV GOPROXY=https://goproxy.cn,direct
WORKDIR /src
# The shared protocol library is a sibling module wired in through a replace
# directive, so the root build context must copy it before go mod download.
COPY lib/wow ./lib/wow
COPY modules/party/go.mod modules/party/go.sum ./modules/party/
WORKDIR /src/modules/party
RUN go mod download
COPY modules/party ./
RUN CGO_ENABLED=0 go build -trimpath -o /module ./cmd

FROM scratch
COPY --from=build /module /module
ENTRYPOINT ["/module"]
