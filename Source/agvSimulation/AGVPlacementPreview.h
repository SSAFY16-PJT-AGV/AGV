
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "AGVPlacementPreview.generated.h"

class UStaticMeshComponent;
class UMaterialInterface;
class AAGVGrabbableObject;

UCLASS()
class AGVSIMULATION_API AAGVPlacementPreview : public AActor
{
    GENERATED_BODY()

public:
    AAGVPlacementPreview();

    // 현재 들고 있는 상품의 메시 복사
    void InitializeFromProduct(
        AAGVGrabbableObject* Product
    );

    // 미리보기 위치 및 표시 상태 갱신
    void UpdatePreview(
        const FTransform& SlotTransform,
        bool bCanPlace
    );

    // 미리보기 숨기기
    void HidePreview();

protected:
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Preview")
    UStaticMeshComponent* PreviewMesh;

    UPROPERTY(EditDefaultsOnly, Category = "Preview")
    UMaterialInterface* ValidMaterial;

    UPROPERTY(EditDefaultsOnly, Category = "Preview")
    UMaterialInterface* InvalidMaterial;
};
